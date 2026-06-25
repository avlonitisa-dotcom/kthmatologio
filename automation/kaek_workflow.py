"""
Main KAEK search, PDF download, and retry workflow.

All portal interaction (search + download) is done via httpx (no browser).
The WAF at ktimatologio.gov.gr blocks headless browsers, so plain HTTP is used.
"""
import asyncio
import logging
import random
import re
from pathlib import Path
from typing import Optional, Callable, Awaitable

from config import Config
from automation.tee_auth import login as tee_login, ensure_logged_in
from automation.tee_search import search_and_download
from database.db import (
    update_kaek_status, increment_retry, add_log,
    get_dashboard_stats,
)

logger = logging.getLogger(__name__)

BroadcastFn = Callable[[dict], Awaitable[None]]


# ── Test KAEK Generator ────────────────────────────────────────────────────────

def generate_test_kaeks(prefix: str = "", count: int = 5) -> list[str]:
    kaeks = []
    for _ in range(count):
        district    = f"{random.randint(1, 99):02d}"
        municipality = f"{random.randint(1, 999):03d}"
        parcel      = f"{random.randint(1000, 9999):04d}"
        sheet       = f"{random.randint(1, 99):02d}"
        kaek = f"{prefix}{district}{municipality}{parcel}/{sheet}/0/0"
        kaeks.append(kaek)
    return kaeks


def sanitize_kaek(raw: str) -> str:
    """Normalize KAEK: strip whitespace, keep digits and slashes."""
    return re.sub(r"[^\d/]", "", raw.strip())


# ── Single KAEK processor ──────────────────────────────────────────────────────

async def process_single_kaek(
    kaek: str,
    broadcast: Optional[BroadcastFn] = None,
) -> dict:
    """
    Full workflow for a single KAEK using httpx (no browser).
    Returns dict with: kaek, success, perigrafiki_ok, xoriki_ok,
                       failure_reason, pdf_perigrafiki_path, pdf_xoriki_path
    """
    kaek_clean = sanitize_kaek(kaek)
    kaek_dir   = Config.DOWNLOADS_DIR / kaek_clean.replace("/", "_")

    async def _broadcast(step: str, status: str, msg: str = "") -> None:
        add_log(kaek_clean, step, status, msg)
        if broadcast:
            await broadcast({
                "type":    "kaek_update",
                "kaek":    kaek_clean,
                "step":    step,
                "status":  status,
                "message": msg,
            })

    update_kaek_status(kaek_clean, "processing")
    await _broadcast("start", "info", f"Starting KAEK {kaek_clean}")

    # Ensure session is alive before each KAEK
    try:
        await ensure_logged_in()
    except Exception as exc:
        await _broadcast("login", "failure", str(exc))
        update_kaek_status(kaek_clean, "failed", failure_reason=str(exc))
        return {
            "kaek": kaek_clean, "success": False,
            "failure_reason": str(exc),
            "perigrafiki_ok": False, "xoriki_ok": False,
        }

    await _broadcast("search", "info", f"Searching portal for {kaek_clean}")

    # httpx-based search + PDF download
    result = await search_and_download(kaek_clean, dest_dir=kaek_dir)

    if result["success"] or result["perigrafiki_ok"] or result["xoriki_ok"]:
        final_status = "completed" if (result["perigrafiki_ok"] and result["xoriki_ok"]) else "partial"
    else:
        final_status = "failed"

    update_kaek_status(
        kaek_clean,
        final_status,
        failure_reason=result.get("failure_reason"),
        pdf_perigrafiki_path=result.get("pdf_perigrafiki_path"),
        pdf_xoriki_path=result.get("pdf_xoriki_path"),
        pdf_perigrafiki_ok=result.get("perigrafiki_ok", False),
        pdf_xoriki_ok=result.get("xoriki_ok", False),
    )

    status_label = "success" if result["success"] else "failure"
    await _broadcast("complete", status_label, final_status)

    # Auto-parse downloaded PDFs
    for pdf_type, path_key in [
        ("perigrafiki", "pdf_perigrafiki_path"),
        ("xoriki",      "pdf_xoriki_path"),
    ]:
        pdf_path = result.get(path_key)
        if pdf_path:
            try:
                from parsing.pdf_parser import parse_pdf_file
                parse_pdf_file(kaek_clean, pdf_type, Path(pdf_path))
            except Exception as parse_exc:
                logger.warning("PDF parse error %s/%s: %s",
                               kaek_clean, pdf_type, parse_exc)

    return result


# ── Batch Processor ────────────────────────────────────────────────────────────

async def process_batch(
    ctx,                               # BrowserContext — kept for API compat, not used
    kaeks: list[str],
    broadcast: Optional[BroadcastFn] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> list[dict]:
    """
    Process a list of KAEKs sequentially with retry logic.
    ctx is accepted for backwards-compatibility but not used (httpx handles everything).
    """
    # Login once at the start
    try:
        await tee_login()
    except Exception as exc:
        add_log(None, "batch", "failure", f"Login failed: {exc}")
        if broadcast:
            await broadcast({"type": "error", "message": f"TEE login failed: {exc}"})
        return []

    results = []

    for kaek in kaeks:
        if stop_event and stop_event.is_set():
            add_log(kaek, "batch", "warning", "Processing stopped by user")
            break

        kaek_clean = sanitize_kaek(kaek)
        logger.info("Processing KAEK %s", kaek_clean)

        last_result = None
        for attempt in range(Config.MAX_RETRIES + 1):
            if attempt > 0:
                increment_retry(kaek_clean)
                add_log(kaek_clean, "retry", "info", f"Retry attempt {attempt}")
                if broadcast:
                    await broadcast({
                        "type": "kaek_retry",
                        "kaek": kaek_clean,
                        "step": "retry",
                        "status": "info",
                        "message": f"Retry {attempt}",
                    })
                await asyncio.sleep(random.uniform(3.0, 6.0))

            try:
                last_result = await process_single_kaek(kaek_clean, broadcast)
                if last_result["success"]:
                    break
                # On failure, re-login before next attempt
                if attempt < Config.MAX_RETRIES:
                    try:
                        await tee_login()
                    except Exception:
                        pass
            except Exception as exc:
                logger.error("Unhandled error for KAEK %s: %s",
                             kaek_clean, exc, exc_info=True)
                update_kaek_status(kaek_clean, "failed", failure_reason=str(exc))
                add_log(kaek_clean, "error", "failure", str(exc))
                last_result = {
                    "kaek": kaek_clean, "success": False,
                    "failure_reason": str(exc),
                    "perigrafiki_ok": False, "xoriki_ok": False,
                }
                try:
                    await tee_login()
                except Exception:
                    pass

        if last_result:
            results.append(last_result)

        if broadcast:
            stats = get_dashboard_stats()
            await broadcast({"type": "stats_update", "stats": stats})

    return results
