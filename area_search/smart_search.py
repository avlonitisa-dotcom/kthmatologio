"""
Smart search orchestrator — full end-to-end flow:
  map discovery → area filtering → TEE download → PDF parsing → results
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

from config import Config
from area_search.map_client import (
    MapParcel, discover_parcels, filter_by_area,
    filter_by_kaek_pattern, stremma_to_sqm,
)
from automation.browser_manager import start_browser, stop_browser, new_context
from automation.tee_auth import login
from automation.kaek_workflow import process_single_kaek, sanitize_kaek
from parsing.pdf_parser import parse_pdf_file
from database import db

logger = logging.getLogger(__name__)

BroadcastFn = Callable[[dict], Awaitable[None]]


@dataclass
class SmartSearchRequest:
    area_name: str = ""
    map_url: str = ""
    target_stremma: Optional[float] = None
    target_sqm: Optional[float] = None
    tolerance_pct: float = 10.0
    min_sqm: Optional[float] = None
    max_sqm: Optional[float] = None
    has_building: str = "any"       # yes | no | any
    has_burdens: str = "any"        # yes | no | any
    kaek_pattern: str = ""
    manual_kaeks: list[str] = field(default_factory=list)
    download_pdfs: bool = True
    max_kaeks: int = 500            # Safety cap


@dataclass
class SmartSearchResult:
    kaek: str
    area_sqm: Optional[float]
    area_stremma: Optional[float]
    municipality: Optional[str]
    has_building: str = "unknown"
    has_burdens: str = "unknown"
    property_type: Optional[str] = None
    burdens_detail: Optional[str] = None
    confidence: float = 0.0
    parse_method: Optional[str] = None
    job_status: str = "pending"
    pdf_perigrafiki_path: Optional[str] = None
    pdf_xoriki_path: Optional[str] = None
    justification: str = ""         # Text snippet explaining classification
    matches_criteria: bool = True


async def run_smart_search(
    req: SmartSearchRequest,
    broadcast: Optional[BroadcastFn] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> list[SmartSearchResult]:
    """
    Full smart search flow. Broadcasts progress events if `broadcast` is given.
    """
    results: list[SmartSearchResult] = []

    async def _bc(type_: str, **kwargs):
        if broadcast:
            await broadcast({"type": type_, **kwargs})

    await _bc("smart_search_start", area_name=req.area_name)

    # ── 1. Resolve area target to sq.m. ────────────────────────────────────────
    if req.target_stremma and not req.target_sqm:
        req.target_sqm = stremma_to_sqm(req.target_stremma)

    # ── 2. Discover parcels from map ───────────────────────────────────────────
    await _bc("phase", phase="discover", message="Ανακάλυψη τεμαχίων από χάρτη…")

    all_parcels: list[MapParcel] = []

    # Use manual KAEKs as additional source (no area data from them)
    for k in req.manual_kaeks:
        all_parcels.append(MapParcel(kaek=sanitize_kaek(k)))

    try:
        # When MAP_BROWSER_ENABLED=false, discover_parcels uses REST only
        # (Nominatim + INSPIRE WFS) — no Chromium needed here.
        map_ctx = None
        if Config.MAP_BROWSER_ENABLED:
            await start_browser()
            map_ctx = await new_context()

        map_parcels = await discover_parcels(
            municipality_name=req.area_name,
            map_url=req.map_url,
            ctx=map_ctx,
        )
        all_parcels.extend(map_parcels)

        if map_ctx:
            await map_ctx.close()
    except Exception as exc:
        logger.warning("Map discovery failed: %s", exc)
        db.add_log(None, "smart_search_discover", "warning", str(exc))
        await _bc("warning", message=f"Αποτυχία ανακάλυψης χάρτη: {exc}")

    await _bc("discover_done", total=len(all_parcels))
    db.add_log(None, "smart_search", "info",
               f"Discovered {len(all_parcels)} parcels for '{req.area_name}'")

    # ── 3. Filter by area ──────────────────────────────────────────────────────
    await _bc("phase", phase="filter", message="Φιλτράρισμα κατά έκταση…")

    filtered = filter_by_area(
        all_parcels,
        target_sqm=req.target_sqm,
        tolerance_pct=req.tolerance_pct,
        min_sqm=req.min_sqm,
        max_sqm=req.max_sqm,
    )
    if req.kaek_pattern:
        filtered = filter_by_kaek_pattern(filtered, req.kaek_pattern)

    # Cap for safety
    if len(filtered) > req.max_kaeks:
        logger.warning("Capping results to %d (found %d)", req.max_kaeks, len(filtered))
        filtered = filtered[:req.max_kaeks]

    await _bc("filter_done", matching=len(filtered), total=len(all_parcels))

    if not filtered:
        await _bc("smart_search_done", results=[])
        return results

    # Add all matching KAEKs to DB
    db.upsert_kaek_batch([p.kaek for p in filtered])

    # ── 4. TEE Login + Download PDFs ───────────────────────────────────────────
    tee_ctx = None
    if req.download_pdfs:
        await _bc("phase", phase="download", message="Σύνδεση στο TEE και λήψη PDF…")

        missing = Config.validate()
        if missing:
            await _bc("error", message=f"Missing credentials: {missing}")
        else:
            try:
                # Always need a fresh browser context for TEE login
                if not Config.MAP_BROWSER_ENABLED:
                    await start_browser()
                tee_ctx = await new_context()
                tee_page = await login(tee_ctx)
                for parcel in filtered:
                    if stop_event and stop_event.is_set():
                        break
                    await process_single_kaek(
                        tee_page, tee_ctx, parcel.kaek, broadcast
                    )
            except Exception as exc:
                logger.error("TEE processing failed: %s", exc, exc_info=True)
                await _bc("error", message=str(exc))

    # ── 5. Parse PDFs & build results ─────────────────────────────────────────
    await _bc("phase", phase="parse", message="Ανάλυση PDF…")

    for parcel in filtered:
        kaek = parcel.kaek
        parse_data = {}

        # Get parse results from DB
        stored = db.get_parse_results(kaek)
        if stored:
            p_data = next((r for r in stored if r["pdf_type"] == "perigrafiki"), {})
            if p_data:
                parse_data = p_data
        else:
            # Try to parse if files exist
            jobs, _ = db.get_all_kaeks()
            job = next((j for j in jobs if j["kaek"] == kaek), None)
            if job:
                for pdf_type, path_field in [
                    ("perigrafiki", "pdf_perigrafiki_path"),
                    ("xoriki", "pdf_xoriki_path"),
                ]:
                    if job.get(path_field):
                        from pathlib import Path
                        d = parse_pdf_file(kaek, pdf_type, Path(job[path_field]))
                        if pdf_type == "perigrafiki":
                            parse_data = d

        # Get job status
        jobs2, _ = db.get_all_kaeks()
        job2 = next((j for j in jobs2 if j["kaek"] == kaek), {})

        # Area: prefer map data, fall back to PDF parse
        area_sqm = parcel.area_sqm or parse_data.get("area_sqm")
        area_stremma = (round(area_sqm / 1000, 4) if area_sqm
                       else parse_data.get("area_stremma"))

        # Build justification snippet
        raw_text = parse_data.get("raw_text", "")
        burdens_detail = parse_data.get("burdens_detail", "")
        justification_parts = []
        if burdens_detail:
            justification_parts.append(f"Βάρη: {burdens_detail[:200]}")
        if parse_data.get("has_burdens") == "no":
            justification_parts.append("Κείμενο PDF: χωρίς βάρη")
        elif parse_data.get("has_burdens") == "unknown" and not raw_text:
            justification_parts.append("PDF δεν αναλύθηκε — άγνωστη κατάσταση")

        sr = SmartSearchResult(
            kaek=kaek,
            area_sqm=area_sqm,
            area_stremma=area_stremma,
            municipality=parcel.municipality or req.area_name,
            has_building=parse_data.get("has_building", "unknown"),
            has_burdens=parse_data.get("has_burdens", "unknown"),
            property_type=parse_data.get("property_type"),
            burdens_detail=burdens_detail or None,
            confidence=parse_data.get("confidence", 0.0),
            parse_method=parse_data.get("parse_method"),
            job_status=job2.get("status", "unknown"),
            pdf_perigrafiki_path=job2.get("pdf_perigrafiki_path"),
            pdf_xoriki_path=job2.get("pdf_xoriki_path"),
            justification=" | ".join(justification_parts) if justification_parts else "Δεν υπάρχουν διαθέσιμα στοιχεία PDF",
        )

        # Apply post-download filters
        if req.has_building != "any" and sr.has_building not in ("unknown", req.has_building):
            sr.matches_criteria = False
        if req.has_burdens != "any" and sr.has_burdens not in ("unknown", req.has_burdens):
            sr.matches_criteria = False

        results.append(sr)

    try:
        if tee_ctx:
            await tee_ctx.close()
        await stop_browser()
    except Exception:
        pass

    await _bc("smart_search_done", results_count=len(results),
              matching=sum(1 for r in results if r.matches_criteria))

    db.add_log(None, "smart_search", "success",
               f"Completed: {len(results)} results, "
               f"{sum(1 for r in results if r.matches_criteria)} matching criteria")

    return results
