"""
Ktimatologio parcel discovery.

Discovery pipeline:
  1. Nominatim (OpenStreetMap) → geocode municipality name → bounding box
  2. Hellenic Cadastre ArcGIS FeatureServer → bbox query → real KAEKs + areas
  3. Browser interception on maps.ktimatologio.gr → capture XHR responses
  4. Manual import fallback
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
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
    # Try progressively broader queries
    queries = [
        f"{name}, Greece",
        f"{name} Αττική Greece",
        f"{name} Greece",
    ]
    headers = {"User-Agent": "TEE-KAEK-Automation/1.0 (authorized research tool)"}

    for q in queries:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(NOMINATIM_URL, params={
                    "q": q, "format": "json", "limit": 5,
                    "addressdetails": 0, "accept-language": "el,en",
                }, headers=headers)
                resp.raise_for_status()
                results = resp.json()
                if not results:
                    continue
                # Prefer results that look like municipalities (suburb, town, city, municipality)
                preferred_types = ("municipality", "city", "town", "village", "suburb", "administrative")
                best = next(
                    (r for r in results if r.get("type") in preferred_types),
                    results[0]
                )
                bb = best.get("boundingbox", [])  # [minlat, maxlat, minlon, maxlon]
                if len(bb) == 4:
                    bbox = {
                        "minlat": float(bb[0]), "maxlat": float(bb[1]),
                        "minlon": float(bb[2]), "maxlon": float(bb[3]),
                        "display_name": best.get("display_name", name),
                    }
                    logger.info("Nominatim bbox for '%s': %s", name, bbox)
                    return bbox
        except Exception as exc:
            logger.warning("Nominatim geocoding failed for '%s': %s", name, exc)

    logger.warning("Nominatim: no results for '%s'", name)
    return None


async def search_municipalities(query: str, limit: int = 10) -> list[dict]:
    """
    Autocomplete search for Greek municipalities via Nominatim.
    Returns list of {name, display_name, bbox} dicts.
    """
    headers = {"User-Agent": "TEE-KAEK-Automation/1.0 (authorized research tool)"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(NOMINATIM_URL, params={
                "q": f"{query} Greece",
                "format": "json",
                "limit": limit,
                "addressdetails": 0,
                "accept-language": "el,en",
                "featuretype": "settlement",
            }, headers=headers)
            resp.raise_for_status()
            results = resp.json()
            out = []
            for r in results:
                bb = r.get("boundingbox", [])
                out.append({
                    "name": r.get("name", query),
                    "display_name": r.get("display_name", ""),
                    "bbox": {
                        "minlat": float(bb[0]), "maxlat": float(bb[1]),
                        "minlon": float(bb[2]), "maxlon": float(bb[3]),
                    } if len(bb) == 4 else None,
                })
            return out
    except Exception as exc:
        logger.warning("Municipality search failed: %s", exc)
        return []


# ── Hellenic Cadastre ArcGIS FeatureServer ─────────────────────────────────────

# Public ArcGIS FeatureServer — returns KAEK + area by bbox (no auth required)
HC_FEATURE_URL = (
    "https://services-eu1.arcgis.com/40tFGWzosjaLJpmn/arcgis/rest/services"
    "/GEOTEMAXIA_APOKLEISTIKES_ON_gdb/FeatureServer/0/query"
)
HC_MAX_RECORDS = 2000  # service limit per request

async def _arcgis_bbox_query(bbox: dict, max_features: int = 2000) -> list[MapParcel]:
    """
    Query the Hellenic Cadastre public ArcGIS FeatureServer with a WGS84 bounding box.
    Paginates automatically if > HC_MAX_RECORDS features are in the bbox.
    Returns list of MapParcel with KAEK and area (m²).
    """
    headers = {"User-Agent": "TEE-KAEK-Automation/1.0"}
    # ESRI envelope: minlon,minlat,maxlon,maxlat
    bbox_str = f"{bbox['minlon']},{bbox['minlat']},{bbox['maxlon']},{bbox['maxlat']}"

    base_params = {
        "geometry": bbox_str,
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
        "outFields": "KAEK,AREA",
        "f": "json",
    }

    parcels: list[MapParcel] = []
    offset = 0
    batch = min(HC_MAX_RECORDS, max_features)

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            while len(parcels) < max_features:
                params = {**base_params, "resultRecordCount": batch, "resultOffset": offset}
                resp = await client.get(HC_FEATURE_URL, params=params, headers=headers)
                if resp.status_code != 200:
                    logger.warning("HC ArcGIS returned %d", resp.status_code)
                    break
                data = resp.json()
                if data.get("error"):
                    logger.warning("HC ArcGIS error: %s", data["error"])
                    break
                features = data.get("features", [])
                if not features:
                    break
                for feat in features:
                    attrs = feat.get("attributes", {})
                    kaek = _clean_kaek(str(attrs.get("KAEK", "") or ""))
                    if not kaek:
                        continue
                    area = attrs.get("AREA")
                    area_sqm = float(area) if area is not None else None
                    parcels.append(MapParcel(kaek=kaek, area_sqm=area_sqm))

                offset += len(features)
                if len(features) < batch:
                    break  # last page

        logger.info("HC ArcGIS: found %d parcels in bbox", len(parcels))
    except Exception as exc:
        logger.warning("HC ArcGIS query failed: %s", exc)

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
                                    "inspire", "cadastral", "geotemaxia"]):
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
        for p in (_parse_json_response(text) or []):
            if p.kaek not in seen:
                seen.add(p.kaek)
                parcels.append(p)

    logger.info("Browser interception: %d unique parcels from %d responses",
                len(parcels), len(captured))
    return parcels


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
        for key in ("AREA", "areaValue", "area", "shape_area", "SHAPE_Area", "Shape__Area"):
            if attrs.get(key) is not None:
                area_sqm = _parse_float(str(attrs[key]))
                if area_sqm:
                    break
        parcels.append(MapParcel(kaek=kaek, area_sqm=area_sqm))
    return parcels


# ── Main discovery entry point ─────────────────────────────────────────────────

async def discover_parcels(
    municipality_name: str = "",
    map_url: str = "",
    ctx: Optional[BrowserContext] = None,
    max_features: int = 2000,
) -> list[MapParcel]:
    """
    Full pipeline: Nominatim → HC ArcGIS FeatureServer → browser interception.
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

    # 1. Nominatim → bbox → HC ArcGIS FeatureServer
    if municipality_name:
        logger.info("Geocoding '%s' via Nominatim…", municipality_name)
        bbox = await geocode_municipality(municipality_name)
        if bbox:
            logger.info("Querying HC ArcGIS FeatureServer for bbox…")
            arcgis_results = await _arcgis_bbox_query(bbox, max_features=max_features)
            _add(arcgis_results)
            logger.info("HC ArcGIS found %d parcels", len(arcgis_results))
        else:
            logger.warning("Could not geocode '%s'", municipality_name)

    # 2. Browser interception (fallback — disabled when MAP_BROWSER_ENABLED=false)
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
