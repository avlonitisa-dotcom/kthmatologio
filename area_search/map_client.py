"""
Ktimatologio parcel discovery — real implementation.

Discovery pipeline:
  1. Nominatim (OpenStreetMap) → geocode municipality name → bounding box
  2. INSPIRE WFS (Hellenic Cadastre) → bbox query → real KAEKs + areas
  3. Browser interception on maps.ktimatologio.gr → capture XHR responses
  4. Manual import fallback

All endpoints used are public / unauthenticated.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional
import httpx
from playwright.async_api import BrowserContext, Response

from config import Config
from automation.browser_manager import human_delay

logger = logging.getLogger(__name__)

# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class MapParcel:
    kaek: str
    area_sqm: Optional[float] = None
    municipality: Optional[str] = None
    raw_attributes: dict = field(default_factory=dict)

    @property
    def area_stremma(self) -> Optional[float]:
        return round(self.area_sqm / 1000, 4) if self.area_sqm else None


# ── Nominatim geocoding ────────────────────────────────────────────────────────

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

async def geocode_municipality(name: str) -> Optional[dict]:
    """
    Use Nominatim to get the bounding box of a Greek municipality.
    Returns dict with keys: minlon, minlat, maxlon, maxlat (WGS84).
    """
    params = {
        "q": f"{name}, Greece",
        "format": "json",
        "limit": 1,
        "addressdetails": 0,
        "accept-language": "el,en",
    }
    headers = {"User-Agent": "TEE-KAEK-Automation/1.0 (authorized research tool)"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(NOMINATIM_URL, params=params, headers=headers)
            resp.raise_for_status()
            results = resp.json()
            if not results:
                logger.warning("Nominatim: no results for '%s'", name)
                return None
            r = results[0]
            bb = r.get("boundingbox", [])  # [minlat, maxlat, minlon, maxlon]
            if len(bb) == 4:
                bbox = {
                    "minlat": float(bb[0]), "maxlat": float(bb[1]),
                    "minlon": float(bb[2]), "maxlon": float(bb[3]),
                    "display_name": r.get("display_name", name),
                }
                logger.info("Nominatim bbox for '%s': %s", name, bbox)
                return bbox
    except Exception as exc:
        logger.warning("Nominatim geocoding failed for '%s': %s", name, exc)
    return None


# ── INSPIRE WFS ────────────────────────────────────────────────────────────────

# Official Hellenic Cadastre INSPIRE WFS endpoint
INSPIRE_WFS_URL = (
    "https://gis.ktimanet.gr/inspire/rest/services/cadastralparcels/"
    "CadastralParcel/MapServer/exts/InspireFeatureDownload/service"
)

# INSPIRE CadastralParcel XML namespace
NS = {
    "wfs":  "http://www.opengis.net/wfs/2.0",
    "cp":   "urn:x-inspire:spec:gmlas:CadastralParcels:3.0",
    "base": "urn:x-inspire:spec:gmlas:BaseTypes:3.3",
    "gml":  "http://www.opengis.net/gml/3.2",
}

async def _wfs_bbox_query(bbox: dict, max_features: int = 500) -> list[MapParcel]:
    """
    Query the INSPIRE WFS with a bounding box.
    Returns list of MapParcel with KAEK and area.
    """
    params = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "typeNames": "cp:CadastralParcel",
        "BBOX": f"{bbox['minlat']},{bbox['minlon']},{bbox['maxlat']},{bbox['maxlon']},EPSG:4326",
        "count": max_features,
        "outputFormat": "application/gml+xml; version=3.2",
    }
    headers = {"User-Agent": "TEE-KAEK-Automation/1.0 (authorized research tool)"}

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(INSPIRE_WFS_URL, params=params, headers=headers)
            if resp.status_code != 200:
                logger.warning("INSPIRE WFS returned %d", resp.status_code)
                return []
            return _parse_inspire_gml(resp.text)
    except Exception as exc:
        logger.warning("INSPIRE WFS query failed: %s", exc)
        return []


def _parse_inspire_gml(xml_text: str) -> list[MapParcel]:
    """Parse INSPIRE GML response to extract KAEK and area."""
    parcels: list[MapParcel] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("GML parse error: %s", exc)
        # Try JSON fallback
        return _parse_json_response(xml_text)

    # Try multiple namespace patterns for robustness
    ns_variants = [
        {"cp": "urn:x-inspire:spec:gmlas:CadastralParcels:3.0",
         "base": "urn:x-inspire:spec:gmlas:BaseTypes:3.3"},
        {"cp": "http://inspire.ec.europa.eu/schemas/cp/4.0",
         "base": "http://inspire.ec.europa.eu/schemas/base/3.3"},
        {"cp": "http://inspire.ec.europa.eu/schemas/cp/3.0",
         "base": "http://inspire.ec.europa.eu/schemas/base/3.3"},
    ]

    for ns in ns_variants:
        members = root.findall(f".//{{{ns['cp']}}}CadastralParcel")
        if members:
            for member in members:
                parcel = _extract_inspire_parcel(member, ns)
                if parcel:
                    parcels.append(parcel)
            logger.info("INSPIRE WFS: parsed %d parcels from GML", len(parcels))
            return parcels

    # Fallback: regex extraction
    kaek_re = re.compile(r'<[^>]*localId[^>]*>([^<]+)</[^>]*>')
    area_re  = re.compile(r'<[^>]*areaValue[^>]*>([0-9.,]+)</[^>]*>')
    for m_kaek in kaek_re.finditer(xml_text):
        raw = m_kaek.group(1).strip()
        kaek = _clean_kaek(raw)
        if not kaek:
            continue
        area_sqm = None
        nearby = xml_text[max(0, m_kaek.start() - 500):m_kaek.end() + 500]
        m_area = area_re.search(nearby)
        if m_area:
            area_sqm = _parse_float(m_area.group(1))
        parcels.append(MapParcel(kaek=kaek, area_sqm=area_sqm))

    return parcels


def _extract_inspire_parcel(elem: ET.Element, ns: dict) -> Optional[MapParcel]:
    """Extract KAEK and area from a single cp:CadastralParcel element."""
    # KAEK from inspireId/localId
    kaek = None
    for path in [
        f".//{{{ns['base']}}}localId",
        f".//{{{ns['cp']}}}label",
        f".//{{{ns['cp']}}}nationalCadastralReference",
    ]:
        el = elem.find(path)
        if el is not None and el.text:
            kaek = _clean_kaek(el.text.strip())
            if kaek:
                break

    if not kaek:
        return None

    # Area
    area_sqm = None
    area_el = elem.find(f".//{{{ns['cp']}}}areaValue")
    if area_el is not None and area_el.text:
        area_sqm = _parse_float(area_el.text)

    return MapParcel(kaek=kaek, area_sqm=area_sqm)


def _parse_json_response(text: str) -> list[MapParcel]:
    """Try to parse JSON (ESRI FeatureSet or GeoJSON) as fallback."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []

    parcels: list[MapParcel] = []
    features = data.get("features", [])
    for feat in features:
        attrs = feat.get("attributes") or feat.get("properties") or {}
        kaek = None
        for key in ("KAEK", "kaek", "localId", "label", "nationalCadastralReference"):
            if attrs.get(key):
                kaek = _clean_kaek(str(attrs[key]))
                if kaek:
                    break
        if not kaek:
            continue
        area_sqm = None
        for key in ("areaValue", "AREA", "area", "shape_area", "SHAPE_Area"):
            if attrs.get(key):
                area_sqm = _parse_float(str(attrs[key]))
                if area_sqm:
                    break
        parcels.append(MapParcel(kaek=kaek, area_sqm=area_sqm))
    return parcels


# ── Browser network interception ───────────────────────────────────────────────

async def discover_via_browser(
    ctx: BrowserContext,
    municipality_name: str = "",
    map_url: str = "",
    wait_s: int = 25,
) -> list[MapParcel]:
    """
    Open maps.ktimatologio.gr in a browser, intercept network responses
    that contain parcel/KAEK data, and parse them.
    """
    parcels: list[MapParcel] = []
    captured: list[str] = []
    lock = asyncio.Lock()

    page = await ctx.new_page()

    async def on_response(response: Response):
        url = response.url.lower()
        if any(k in url for k in ["query", "parcel", "kaek", "wfs", "getfeature",
                                    "arcgis", "mapserver", "featureserver", "geoserver",
                                    "inspire", "cadastral"]):
            try:
                ct = response.headers.get("content-type", "")
                if any(t in ct for t in ["json", "xml", "gml"]):
                    text = await response.text()
                    if len(text) > 100:
                        async with lock:
                            captured.append(text)
            except Exception:
                pass

    page.on("response", on_response)

    target = map_url or (
        f"{Config.MAP_BASE_URL}?locale=el"
        "#widget_6=active_datasource_id:dataSource_1"
    )

    try:
        await page.goto(target, wait_until="networkidle", timeout=Config.PAGE_LOAD_TIMEOUT)
        await human_delay(3.0, 5.0)

        if municipality_name:
            for sel in [
                'input[placeholder*="αναζήτηση" i]',
                'input[placeholder*="search" i]',
                'input[type="search"]',
                '.esri-search__input',
            ]:
                try:
                    el = await page.wait_for_selector(sel, timeout=4000)
                    if el:
                        await el.fill(municipality_name)
                        await el.press("Enter")
                        await human_delay(2.0, 4.0)
                        break
                except Exception:
                    continue

        await human_delay(wait_s * 0.4, wait_s * 0.6)

    except Exception as exc:
        logger.warning("Browser map navigation error: %s", exc)
    finally:
        await page.close()

    seen: set[str] = set()
    for text in captured:
        for p in _parse_json_response(text) or _parse_inspire_gml(text):
            if p.kaek not in seen:
                seen.add(p.kaek)
                parcels.append(p)

    logger.info("Browser interception: %d unique parcels from %d responses",
                len(parcels), len(captured))
    return parcels


# ── Main discovery entry point ─────────────────────────────────────────────────

async def discover_parcels(
    municipality_name: str = "",
    map_url: str = "",
    ctx: Optional[BrowserContext] = None,
) -> list[MapParcel]:
    """
    Full pipeline: Nominatim → INSPIRE WFS → browser interception.
    Returns list of MapParcel with real KAEK + area where available.
    """
    parcels: list[MapParcel] = []
    seen_kaeks: set[str] = set()

    def _add(new: list[MapParcel]):
        for p in new:
            if p.kaek not in seen_kaeks:
                seen_kaeks.add(p.kaek)
                parcels.append(p)
            elif p.area_sqm:
                for ep in parcels:
                    if ep.kaek == p.kaek and not ep.area_sqm:
                        ep.area_sqm = p.area_sqm
                        break

    # 1. Nominatim → bbox → INSPIRE WFS
    if municipality_name:
        logger.info("Geocoding '%s' via Nominatim…", municipality_name)
        bbox = await geocode_municipality(municipality_name)
        if bbox:
            logger.info("Querying INSPIRE WFS for bbox %s…", bbox)
            wfs_results = await _wfs_bbox_query(bbox)
            _add(wfs_results)
            logger.info("INSPIRE WFS found %d parcels", len(wfs_results))

    # 2. Browser interception (supplement or fallback — disabled when MAP_BROWSER_ENABLED=false)
    from config import Config
    if ctx and len(parcels) < 5 and Config.MAP_BROWSER_ENABLED:
        logger.info("Trying browser interception on maps.ktimatologio.gr…")
        browser_results = await discover_via_browser(ctx, municipality_name, map_url)
        _add(browser_results)
    elif not Config.MAP_BROWSER_ENABLED:
        logger.info("Browser map discovery disabled (MAP_BROWSER_ENABLED=false)")

    logger.info("Total parcels for '%s': %d (with area: %d)",
                municipality_name, len(parcels),
                sum(1 for p in parcels if p.area_sqm))
    return parcels


# ── Area / KAEK filtering ──────────────────────────────────────────────────────

def filter_by_area(
    parcels: list[MapParcel],
    target_sqm: Optional[float] = None,
    tolerance_pct: float = 10.0,
    min_sqm: Optional[float] = None,
    max_sqm: Optional[float] = None,
) -> list[MapParcel]:
    if not (target_sqm or min_sqm or max_sqm):
        return parcels

    lo = min_sqm
    hi = max_sqm
    if target_sqm and not (min_sqm or max_sqm):
        f = tolerance_pct / 100.0
        lo = target_sqm * (1 - f)
        hi = target_sqm * (1 + f)

    matched = []
    for p in parcels:
        if p.area_sqm is None:
            matched.append(p)  # keep unknowns
            continue
        if (lo is None or p.area_sqm >= lo) and (hi is None or p.area_sqm <= hi):
            matched.append(p)

    logger.info("Area filter %.0f–%.0f sqm: %d/%d match",
                lo or 0, hi or 0, len(matched), len(parcels))
    return matched


def filter_by_kaek_pattern(parcels: list[MapParcel], pattern: str) -> list[MapParcel]:
    if not pattern:
        return parcels
    return [p for p in parcels if p.kaek.endswith(pattern)]


# ── Utilities ──────────────────────────────────────────────────────────────────

def stremma_to_sqm(stremma: float) -> float:
    return stremma * 1000.0


def parse_kaek_list_from_text(text: str) -> list[str]:
    pattern = re.compile(r"\b(\d{4,}/\d+/\d+/\d+)\b")
    seen: set[str] = set()
    result: list[str] = []
    for m in pattern.finditer(text):
        k = m.group(1).strip()
        if k not in seen:
            seen.add(k)
            result.append(k)
    return result


def _clean_kaek(raw: str) -> str:
    """Normalize KAEK — strip namespace prefix like 'KAEK.' or 'GR.'"""
    raw = re.sub(r"^(KAEK\.|GR\.[^.]+\.)", "", raw.strip())
    return re.sub(r"[^\d/]", "", raw) or ""


def _parse_float(s: str) -> Optional[float]:
    try:
        v = float(str(s).replace(",", ".").replace(" ", ""))
        return v if 0 < v < 10_000_000 else None
    except (ValueError, TypeError):
        return None
