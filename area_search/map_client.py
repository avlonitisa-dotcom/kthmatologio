"""
Ktimatologio map integration — parcel discovery with area data.

Discovery order (per spec):
  1. Intercept public WFS/REST network calls from maps.ktimatologio.gr
     to extract KAEK + area directly from the map's own responses.
  2. If that yields nothing, provide semi-automated mode:
     user selects parcels manually; we capture visible info.
  3. Fallback: user imports/pastes KAEK list manually.

After discovery, parcels are filtered by area criteria BEFORE any TEE login.
Only matching KAEKs are sent for TEE download.

IMPORTANT: Only public, unauthenticated map data is used here.
           Do not bypass any security or auth controls.
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

# ── Data Model ─────────────────────────────────────────────────────────────────

@dataclass
class MapParcel:
    """A parcel discovered from the map layer."""
    kaek: str
    area_sqm: Optional[float] = None
    municipality: Optional[str] = None
    raw_attributes: dict = field(default_factory=dict)

    @property
    def area_stremma(self) -> Optional[float]:
        return round(self.area_sqm / 1000, 4) if self.area_sqm else None


# ── Known public endpoints ─────────────────────────────────────────────────────
# Inspect actual network traffic at maps.ktimatologio.gr → DevTools → Network
# and update these endpoints with the real ones you observe.

# ESRI MapServer / FeatureServer patterns (common for Greek government GIS)
_ESRI_ENDPOINTS = [
    "https://gis.ktimatologio.gr/arcgis/rest/services",
    "https://services.ktimatologio.gr/arcgis/rest/services",
]

# WFS endpoint (alternative for OGC-compliant portals)
_WFS_ENDPOINT = "https://gis.ktimatologio.gr/geoserver/wfs"

# Keywords that identify parcel responses in intercepted traffic
_PARCEL_RESPONSE_KEYWORDS = [
    "kaek", "KAEK", "parcels", "parcel", "αγροτεμάχιο",
    "FeatureCollection", "features", "geometry",
]

# Field name variants for KAEK in different API responses
_KAEK_FIELD_NAMES = ["KAEK", "kaek", "KAEK_ID", "cadastral_id", "id", "parcel_id"]

# Field name variants for area
_AREA_FIELD_NAMES = ["AREA", "area", "EMVADON", "emvadon", "shape_area",
                     "SHAPE_Area", "sqm", "area_sqm"]

# Municipality field variants
_MUNI_FIELD_NAMES = ["OTA", "ota", "MUNICIPALITY", "municipality", "DIMOS", "dimos",
                     "PERIFEREIA", "NAME"]


def _normalize_number(val) -> Optional[float]:
    if val is None:
        return None
    try:
        if isinstance(val, (int, float)):
            v = float(val)
            return v if v > 0 else None
        s = str(val).strip().replace(".", "").replace(",", ".")
        v = float(s)
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


def _parse_features(data: dict | list) -> list[MapParcel]:
    """Extract MapParcel objects from any GeoJSON / ESRI JSON structure."""
    parcels = []

    # Handle ESRI FeatureSet: {"features": [{"attributes": {...}}]}
    if isinstance(data, dict):
        features = data.get("features", [])
        if not features:
            # Maybe it's a single feature
            if "attributes" in data:
                features = [data]
            elif "KAEK" in data or "kaek" in data:
                features = [{"attributes": data}]
    elif isinstance(data, list):
        features = data
    else:
        return parcels

    for feat in features:
        attrs = feat.get("attributes") or feat.get("properties") or feat
        if not isinstance(attrs, dict):
            continue

        # Extract KAEK
        kaek = None
        for name in _KAEK_FIELD_NAMES:
            v = attrs.get(name)
            if v:
                kaek = str(v).strip()
                break

        if not kaek:
            continue

        # Validate KAEK format loosely
        if not re.search(r"\d{4,}", kaek):
            continue

        # Extract area
        area_sqm = None
        for name in _AREA_FIELD_NAMES:
            v = attrs.get(name)
            area_sqm = _normalize_number(v)
            if area_sqm and 1 < area_sqm < 1_000_000:
                break

        # Extract municipality
        muni = None
        for name in _MUNI_FIELD_NAMES:
            v = attrs.get(name)
            if v:
                muni = str(v).strip()
                break

        parcels.append(MapParcel(
            kaek=kaek,
            area_sqm=area_sqm,
            municipality=muni,
            raw_attributes=attrs,
        ))

    return parcels


def _extract_from_text(text: str) -> list[MapParcel]:
    """
    Extract KAEKs (and optionally area) from raw JSON response text.
    Handles embedded KAEK patterns even inside larger JSON blobs.
    """
    parcels: list[MapParcel] = []
    try:
        data = json.loads(text)
        return _parse_features(data)
    except json.JSONDecodeError:
        pass

    # Fallback: regex extraction from raw text
    kaek_pattern = re.compile(r'"(?:KAEK|kaek)":\s*"([^"]+)"')
    area_pattern = re.compile(r'"(?:AREA|area|EMVADON|shape_area)":\s*([\d.]+)')

    for m_kaek in kaek_pattern.finditer(text):
        kaek = m_kaek.group(1)
        area_sqm = None
        # Try to find nearby area value
        nearby = text[max(0, m_kaek.start()-200):m_kaek.end()+200]
        m_area = area_pattern.search(nearby)
        if m_area:
            area_sqm = _normalize_number(m_area.group(1))

        parcels.append(MapParcel(kaek=kaek, area_sqm=area_sqm))

    return parcels


# ── REST API Discovery ─────────────────────────────────────────────────────────

async def _rest_search_municipality(municipality_name: str) -> list[MapParcel]:
    """
    Query the public Ktimatologio ESRI REST service for parcels by municipality.
    Tries common field/endpoint patterns. Update after inspecting live API.
    """
    parcels: list[MapParcel] = []

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for base in _ESRI_ENDPOINTS:
            for layer_id in range(0, 5):
                url = f"{base}/MapServer/{layer_id}/query"
                params = {
                    "where": f"UPPER(OTA) LIKE UPPER('%{municipality_name}%')",
                    "outFields": ",".join(_KAEK_FIELD_NAMES + _AREA_FIELD_NAMES + _MUNI_FIELD_NAMES),
                    "returnGeometry": "false",
                    "f": "json",
                    "resultRecordCount": 2000,
                }
                try:
                    resp = await client.get(url, params=params, timeout=10)
                    if resp.status_code == 200:
                        found = _parse_features(resp.json())
                        if found:
                            logger.info(
                                "REST layer %s/%d returned %d parcels",
                                base, layer_id, len(found)
                            )
                            parcels.extend(found)
                except Exception as exc:
                    logger.debug("REST endpoint %s layer %d: %s", base, layer_id, exc)

    return parcels


# ── Browser Network Interception ───────────────────────────────────────────────

async def discover_parcels_via_browser(
    ctx: BrowserContext,
    municipality_name: str = "",
    map_url: str = "",
    timeout_s: int = 30,
) -> list[MapParcel]:
    """
    Open the Ktimatologio map in a browser, capture all XHR/fetch responses
    that look like parcel data, and parse KAEKs + areas from them.

    If municipality_name is given, also tries searching via the map's UI.
    """
    parcels: list[MapParcel] = []
    captured: list[str] = []
    capture_lock = asyncio.Lock()

    page = await ctx.new_page()

    async def capture_response(response: Response):
        url = response.url.lower()
        if any(kw in url for kw in ["query", "parcel", "kaek", "wfs", "getfeature",
                                     "arcgis", "mapserver", "featureserver", "geoserver"]):
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct or "xml" in ct:
                    text = await response.text()
                    if any(kw in text for kw in _PARCEL_RESPONSE_KEYWORDS):
                        async with capture_lock:
                            captured.append(text)
                        logger.debug("Captured parcel response from %s (%d chars)",
                                     response.url[:80], len(text))
            except Exception:
                pass

    page.on("response", capture_response)

    target_url = map_url or (
        f"{Config.MAP_BASE_URL}?locale=el"
        f"#widget_6=active_datasource_id:dataSource_1"
    )

    try:
        await page.goto(target_url, wait_until="networkidle", timeout=Config.PAGE_LOAD_TIMEOUT)
        await human_delay(3.0, 5.0)

        # Try searching in the map UI if municipality name given
        if municipality_name:
            search_selectors = [
                'input[placeholder*="αναζήτηση" i]',
                'input[placeholder*="search" i]',
                'input[type="search"]',
                '.esri-search__input',
                '.jimu-widget-search input',
            ]
            for sel in search_selectors:
                try:
                    el = await page.wait_for_selector(sel, timeout=5000)
                    if el:
                        await el.fill(municipality_name)
                        await el.press("Enter")
                        await human_delay(2.0, 4.0)
                        logger.info("Typed '%s' into map search", municipality_name)
                        break
                except Exception:
                    continue

            # Also try clicking result suggestions
            suggestion_selectors = [
                ".esri-search__suggestion-list li:first-child",
                ".jimu-search-result li:first-child",
                ".search-result:first-child",
            ]
            for sel in suggestion_selectors:
                try:
                    el = await page.wait_for_selector(sel, timeout=3000)
                    if el:
                        await el.click()
                        await human_delay(2.0, 4.0)
                        break
                except Exception:
                    continue

        # Wait for more responses
        await human_delay(timeout_s * 0.3, timeout_s * 0.5)

    except Exception as exc:
        logger.warning("Browser map navigation error: %s", exc)
    finally:
        await page.close()

    # Parse all captured responses
    seen_kaeks: set[str] = set()
    for text in captured:
        for p in _extract_from_text(text):
            if p.kaek not in seen_kaeks:
                seen_kaeks.add(p.kaek)
                parcels.append(p)

    logger.info(
        "Browser interception found %d unique parcels from %d responses",
        len(parcels), len(captured)
    )
    return parcels


# ── Main Discovery Entry Point ─────────────────────────────────────────────────

async def discover_parcels(
    municipality_name: str = "",
    map_url: str = "",
    ctx: Optional[BrowserContext] = None,
) -> list[MapParcel]:
    """
    Full discovery pipeline. Returns list of MapParcel objects with KAEK + area.
    """
    parcels: list[MapParcel] = []

    # 1. REST API
    if municipality_name:
        try:
            rest_results = await _rest_search_municipality(municipality_name)
            parcels.extend(rest_results)
        except Exception as exc:
            logger.debug("REST discovery failed: %s", exc)

    # 2. Browser interception (if no REST results or to supplement)
    if ctx and len(parcels) < 5:
        try:
            browser_results = await discover_parcels_via_browser(
                ctx, municipality_name, map_url
            )
            # Merge, avoiding duplicates; prefer browser results that have area data
            existing_kaeks = {p.kaek for p in parcels}
            for p in browser_results:
                if p.kaek not in existing_kaeks:
                    parcels.append(p)
                    existing_kaeks.add(p.kaek)
                elif p.area_sqm:
                    # Update existing with area if we now have it
                    for ep in parcels:
                        if ep.kaek == p.kaek and not ep.area_sqm:
                            ep.area_sqm = p.area_sqm
                            ep.municipality = ep.municipality or p.municipality
        except Exception as exc:
            logger.warning("Browser discovery failed: %s", exc)

    logger.info(
        "Total parcels discovered for '%s': %d (with area: %d)",
        municipality_name,
        len(parcels),
        sum(1 for p in parcels if p.area_sqm),
    )
    return parcels


# ── Area Filtering ─────────────────────────────────────────────────────────────

def filter_by_area(
    parcels: list[MapParcel],
    target_sqm: Optional[float],
    tolerance_pct: float = 10.0,
    min_sqm: Optional[float] = None,
    max_sqm: Optional[float] = None,
) -> list[MapParcel]:
    """
    Keep only parcels matching the area criteria.
    If target_sqm is given, applies ± tolerance_pct.
    min_sqm / max_sqm provide absolute bounds (override tolerance).
    """
    if not (target_sqm or min_sqm or max_sqm):
        return parcels  # No filter — return all

    lo = min_sqm
    hi = max_sqm

    if target_sqm and not (min_sqm or max_sqm):
        factor = tolerance_pct / 100.0
        lo = target_sqm * (1 - factor)
        hi = target_sqm * (1 + factor)

    matched = []
    for p in parcels:
        if p.area_sqm is None:
            # Keep unknowns with a flag rather than discarding
            matched.append(p)
            continue
        in_range = True
        if lo is not None and p.area_sqm < lo:
            in_range = False
        if hi is not None and p.area_sqm > hi:
            in_range = False
        if in_range:
            matched.append(p)

    logger.info(
        "Area filter (%.0f–%.0f sqm): %d/%d parcels match",
        lo or 0, hi or 0, len(matched), len(parcels)
    )
    return matched


def filter_by_kaek_pattern(parcels: list[MapParcel], pattern: str) -> list[MapParcel]:
    if not pattern:
        return parcels
    return [p for p in parcels if p.kaek.endswith(pattern)]


# ── Text parsing utilities ─────────────────────────────────────────────────────

def parse_kaek_list_from_text(text: str) -> list[str]:
    pattern = re.compile(r"\b(\d{4,}\d*/\d+/\d+/\d+)\b")
    seen: set[str] = set()
    kaeks: list[str] = []
    for m in pattern.finditer(text):
        k = m.group(1).strip()
        if k not in seen:
            seen.add(k)
            kaeks.append(k)
    return kaeks


def stremma_to_sqm(stremma: float) -> float:
    return stremma * 1000.0
