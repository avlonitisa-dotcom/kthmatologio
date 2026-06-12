"""
Main KAEK search, PDF download, and retry workflow.
"""
import asyncio
import logging
import random
import re
import string
from pathlib import Path
from typing import Optional, Callable, Awaitable

from playwright.async_api import Page, BrowserContext, TimeoutError as PWTimeout

from config import Config
from automation.browser_manager import (
    human_delay, safe_click, safe_fill,
    screenshot_on_error, wait_for_any, wait_for_download,
    page_has_text,
)
from automation.selectors import SEARCH, PDF, STATES
from automation.tee_auth import navigate_to_kaek_search, ensure_session_alive
from database.db import (
    update_kaek_status, increment_retry, add_log,
    get_pending_kaeks, get_dashboard_stats,
)

logger = logging.getLogger(__name__)

# Callable type for broadcasting status updates over WebSocket
BroadcastFn = Callable[[dict], Awaitable[None]]


# ── Test KAEK Generator ────────────────────────────────────────────────────────

def generate_test_kaeks(prefix: str = "", count: int = 5) -> list[str]:
    """
    Generate syntactically plausible test KAEKs ending in /0/0.
    Format: {district_code}{municipality_code}{parcel}/{sheet}/0/0
    These are for UI flow testing ONLY, not real searches.
    """
    kaeks = []
    for _ in range(count):
        district = f"{random.randint(1, 99):02d}"
        municipality = f"{random.randint(1, 999):03d}"
        parcel = f"{random.randint(1000, 9999):04d}"
        sheet = f"{random.randint(1, 99):02d}"
        kaek = f"{prefix}{district}{municipality}{parcel}/{sheet}/0/0"
        kaeks.append(kaek)
    return kaeks


def sanitize_kaek(raw: str) -> str:
    """Normalize KAEK: strip whitespace, keep digits and slashes."""
    return re.sub(r"[^\d/]", "", raw.strip())


# ── PDF Download ───────────────────────────────────────────────────────────────

async def download_pdf(
    page: Page,
    kaek: str,
    selector_set,
    dest_path: Path,
    pdf_label: str,
) -> bool:
    """
    Click a PDF link, wait for the download, and save it.
    Returns True on success.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Try clicking the link and intercepting the download
    all_selectors = [selector_set.primary] + selector_set.fallbacks

    for sel in all_selectors:
        try:
            el = await page.wait_for_selector(sel, timeout=8000, state="visible")
            if not el:
                continue

            # Try direct download interception first
            try:
                async with page.expect_download(timeout=60000) as dl_info:
                    await el.click()
                download = await dl_info.value
                await download.save_as(str(dest_path))
                add_log(kaek, f"download_{pdf_label}", "success", f"Saved to {dest_path}")
                return True
            except Exception:
                # Fallback: PDF may open in a new tab/iframe, try to get href
                href = await el.get_attribute("href")
                if href:
                    pdf_page = await page.context.new_page()
                    try:
                        response = await pdf_page.goto(href, wait_until="networkidle")
                        if response and response.ok:
                            body = await response.body()
                            dest_path.write_bytes(body)
                            add_log(kaek, f"download_{pdf_label}", "success", f"Saved via href to {dest_path}")
                            return True
                    finally:
                        await pdf_page.close()

        except PWTimeout:
            continue
        except Exception as exc:
            logger.debug("download_pdf selector %s: %s", sel, exc)
            continue

    add_log(kaek, f"download_{pdf_label}", "failure", "No PDF link found with any selector")
    return False


# ── KAEK Processing ────────────────────────────────────────────────────────────

async def process_single_kaek(
    page: Page,
    ctx: BrowserContext,
    kaek: str,
    broadcast: Optional[BroadcastFn] = None,
) -> dict:
    """
    Full workflow for a single KAEK.
    Returns a result dict with keys: kaek, success, perigrafiki_ok, xoriki_ok,
    failure_reason, pdf_perigrafiki_path, pdf_xoriki_path.
    """
    result = {
        "kaek": kaek,
        "success": False,
        "perigrafiki_ok": False,
        "xoriki_ok": False,
        "failure_reason": None,
        "pdf_perigrafiki_path": None,
        "pdf_xoriki_path": None,
    }

    kaek_dir = Config.DOWNLOADS_DIR / kaek.replace("/", "_")

    async def _broadcast(step: str, status: str, msg: str = "") -> None:
        add_log(kaek, step, status, msg)
        if broadcast:
            await broadcast({
                "type": "kaek_update",
                "kaek": kaek,
                "step": step,
                "status": status,
                "message": msg,
            })

    update_kaek_status(kaek, "processing")
    await _broadcast("start", "info", f"Starting KAEK {kaek}")

    # Ensure session is alive
    page = await ensure_session_alive(page, ctx)

    # Navigate to search form (re-navigate each time for reliability)
    nav_ok = await navigate_to_kaek_search(page)
    if not nav_ok:
        result["failure_reason"] = "Navigation to KAEK search failed"
        await _broadcast("navigate", "failure", result["failure_reason"])
        return result

    await human_delay()

    # Fill KAEK input
    kaek_clean = sanitize_kaek(kaek)
    fill_ok = await safe_fill(page, SEARCH["kaek_input"], kaek_clean)
    if not fill_ok:
        await screenshot_on_error(page, kaek_clean, "kaek_input_missing")
        result["failure_reason"] = "KAEK input field not found"
        await _broadcast("fill_kaek", "failure", result["failure_reason"])
        return result

    await _broadcast("fill_kaek", "success")
    await human_delay(0.5, 1.0)

    # Submit search
    submit_ok = await safe_click(page, SEARCH["submit"])
    if not submit_ok:
        await screenshot_on_error(page, kaek_clean, "search_submit_missing")
        result["failure_reason"] = "Search submit button not found"
        await _broadcast("submit_search", "failure", result["failure_reason"])
        return result

    # Wait for results
    try:
        await page.wait_for_load_state("networkidle", timeout=Config.PAGE_LOAD_TIMEOUT)
    except PWTimeout:
        pass
    await human_delay(1.0, 2.0)

    # Check for no results
    no_results_selectors = [STATES["no_results"].primary] + STATES["no_results"].fallbacks
    for sel in no_results_selectors:
        el = await page.query_selector(sel)
        if el and await el.is_visible():
            result["failure_reason"] = "No results found for KAEK"
            await _broadcast("results_check", "failure", result["failure_reason"])
            return result

    await _broadcast("results_check", "success", "Results found")

    # Click magnifying glass / detail icon
    mag_ok = await safe_click(page, SEARCH["magnifier_icon"])
    if not mag_ok:
        # Try clicking the first result row directly
        row_ok = await safe_click(page, SEARCH["result_row"])
        if not row_ok:
            await screenshot_on_error(page, kaek_clean, "magnifier_missing")
            result["failure_reason"] = "Cannot open KAEK detail (magnifier/row click failed)"
            await _broadcast("open_detail", "failure", result["failure_reason"])
            return result

    try:
        await page.wait_for_load_state("networkidle", timeout=Config.PAGE_LOAD_TIMEOUT)
    except PWTimeout:
        pass
    await human_delay(1.5, 2.5)

    await _broadcast("open_detail", "success", "Detail page opened")

    # ── Download PDFs ──────────────────────────────────────────────────────────

    perigrafiki_path = kaek_dir / f"{kaek_clean}_perigrafiki_vasi.pdf"
    xoriki_path = kaek_dir / f"{kaek_clean}_xoriki_vasi.pdf"

    # Download ΑΠΟΣΠΑΣΜΑ ΠΕΡΙΓΡΑΦΙΚΗΣ ΒΑΣΗΣ
    await _broadcast("download_perigrafiki", "info", "Downloading ΑΠΟΣΠΑΣΜΑ ΠΕΡΙΓΡΑΦΙΚΗΣ ΒΑΣΗΣ")
    p_ok = await download_pdf(page, kaek, PDF["perigrafiki_link"], perigrafiki_path, "perigrafiki")
    result["perigrafiki_ok"] = p_ok
    if p_ok:
        result["pdf_perigrafiki_path"] = str(perigrafiki_path)
        await _broadcast("download_perigrafiki", "success", str(perigrafiki_path))
    else:
        await screenshot_on_error(page, kaek_clean, "perigrafiki_download_failed")
        await _broadcast("download_perigrafiki", "failure", "PDF not found or download failed")

    await human_delay()

    # Download ΑΠΟΣΠΑΣΜΑ ΧΩΡΙΚΗΣ ΒΑΣΗΣ
    await _broadcast("download_xoriki", "info", "Downloading ΑΠΟΣΠΑΣΜΑ ΧΩΡΙΚΗΣ ΒΑΣΗΣ")
    x_ok = await download_pdf(page, kaek, PDF["xoriki_link"], xoriki_path, "xoriki")
    result["xoriki_ok"] = x_ok
    if x_ok:
        result["pdf_xoriki_path"] = str(xoriki_path)
        await _broadcast("download_xoriki", "success", str(xoriki_path))
    else:
        await screenshot_on_error(page, kaek_clean, "xoriki_download_failed")
        await _broadcast("download_xoriki", "failure", "PDF not found or download failed")

    # Determine final status
    if p_ok and x_ok:
        result["success"] = True
        final_status = "completed"
    elif p_ok or x_ok:
        final_status = "partial"
        result["failure_reason"] = "One PDF missing"
    else:
        final_status = "failed"
        result["failure_reason"] = "Both PDFs failed to download"

    update_kaek_status(
        kaek,
        final_status,
        failure_reason=result["failure_reason"],
        pdf_perigrafiki_path=result["pdf_perigrafiki_path"],
        pdf_xoriki_path=result["pdf_xoriki_path"],
        pdf_perigrafiki_ok=p_ok,
        pdf_xoriki_ok=x_ok,
    )

    await _broadcast("complete", "success" if result["success"] else "failure",
                     final_status)

    await human_delay()  # Pace before next KAEK
    return result


# ── Batch Processor ────────────────────────────────────────────────────────────

async def process_batch(
    ctx: BrowserContext,
    kaeks: list[str],
    broadcast: Optional[BroadcastFn] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> list[dict]:
    """
    Process a list of KAEKs sequentially with retry logic.
    """
    from automation.tee_auth import login
    from parsing.pdf_parser import parse_pdf_file

    page = await login(ctx)
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
                retry_count = increment_retry(kaek_clean)
                add_log(kaek_clean, "retry", "info", f"Retry attempt {attempt}")
                if broadcast:
                    await broadcast({"type": "kaek_retry", "kaek": kaek_clean, "attempt": attempt})
                await human_delay(3.0, 6.0)  # Longer delay before retry

            try:
                last_result = await process_single_kaek(page, ctx, kaek_clean, broadcast)
                if last_result["success"]:
                    break
            except Exception as exc:
                logger.error("Unhandled error for KAEK %s: %s", kaek_clean, exc, exc_info=True)
                await screenshot_on_error(page, kaek_clean, f"unhandled_error_attempt{attempt}")
                update_kaek_status(kaek_clean, "failed", failure_reason=str(exc))
                add_log(kaek_clean, "error", "failure", str(exc))
                last_result = {
                    "kaek": kaek_clean, "success": False,
                    "failure_reason": str(exc),
                    "perigrafiki_ok": False, "xoriki_ok": False,
                }
                # Try to re-login on unexpected errors
                try:
                    page = await login(ctx)
                except Exception:
                    pass

        if last_result:
            results.append(last_result)

            # Auto-parse downloaded PDFs
            for pdf_type, path_key in [
                ("perigrafiki", "pdf_perigrafiki_path"),
                ("xoriki", "pdf_xoriki_path"),
            ]:
                pdf_path = last_result.get(path_key)
                if pdf_path:
                    try:
                        parse_pdf_file(kaek_clean, pdf_type, Path(pdf_path))
                    except Exception as parse_exc:
                        logger.warning("PDF parse error %s/%s: %s", kaek_clean, pdf_type, parse_exc)

        if broadcast:
            stats = get_dashboard_stats()
            await broadcast({"type": "stats_update", "stats": stats})

    return results
