"""
Playwright browser context management with human-like pacing and error helpers.
"""
import asyncio
import logging
import random
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
)

from config import Config
from automation.selectors import SelectorSet

logger = logging.getLogger(__name__)

_playwright: Optional[Playwright] = None
_browser: Optional[Browser] = None


async def start_browser() -> None:
    global _playwright, _browser
    _playwright = await async_playwright().start()
    launcher = getattr(_playwright, Config.BROWSER_TYPE)
    _browser = await launcher.launch(
        headless=Config.HEADLESS,
        args=["--disable-blink-features=AutomationControlled"],
    )
    logger.info("Browser started (%s, headless=%s)", Config.BROWSER_TYPE, Config.HEADLESS)


async def stop_browser() -> None:
    global _playwright, _browser
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None
    logger.info("Browser stopped")


async def new_context() -> BrowserContext:
    if _browser is None:
        raise RuntimeError("Browser not started. Call start_browser() first.")
    ctx = await _browser.new_context(
        viewport={"width": 1280, "height": 900},
        locale="el-GR",
        timezone_id="Europe/Athens",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    ctx.set_default_timeout(Config.ELEMENT_TIMEOUT)
    ctx.set_default_navigation_timeout(Config.PAGE_LOAD_TIMEOUT)
    return ctx


async def human_delay(min_s: Optional[float] = None, max_s: Optional[float] = None) -> None:
    lo = min_s if min_s is not None else Config.MIN_DELAY
    hi = max_s if max_s is not None else Config.MAX_DELAY
    await asyncio.sleep(random.uniform(lo, hi))


async def safe_click(page: Page, selector: SelectorSet, timeout: Optional[int] = None) -> bool:
    """Try primary selector then fallbacks. Returns True on success."""
    t = timeout or Config.ELEMENT_TIMEOUT
    all_selectors = [selector.primary] + selector.fallbacks
    for sel in all_selectors:
        try:
            await page.wait_for_selector(sel, timeout=t, state="visible")
            await page.click(sel)
            return True
        except PWTimeout:
            continue
        except Exception as exc:
            logger.debug("safe_click %s failed with %s: %s", sel, type(exc).__name__, exc)
            continue
    return False


async def safe_fill(page: Page, selector: SelectorSet, value: str) -> bool:
    """Fill input using primary then fallback selectors."""
    all_selectors = [selector.primary] + selector.fallbacks
    for sel in all_selectors:
        try:
            await page.wait_for_selector(sel, timeout=Config.ELEMENT_TIMEOUT, state="visible")
            await page.fill(sel, value)
            return True
        except PWTimeout:
            continue
        except Exception as exc:
            logger.debug("safe_fill %s failed: %s", sel, exc)
            continue
    return False


async def wait_for_any(
    page: Page,
    selectors: list[str],
    timeout: int = 15000,
) -> Optional[str]:
    """Return the first selector from the list that becomes visible."""
    deadline = asyncio.get_event_loop().time() + timeout / 1000
    while asyncio.get_event_loop().time() < deadline:
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    return sel
            except Exception:
                pass
        await asyncio.sleep(0.3)
    return None


async def page_has_text(page: Page, text: str) -> bool:
    try:
        return bool(await page.query_selector(f"text={text}"))
    except Exception:
        return False


async def screenshot_on_error(page: Page, kaek: str, step: str) -> Optional[Path]:
    """Save a screenshot to errors/{kaek}/ and return the path."""
    try:
        error_dir = Config.ERRORS_DIR / kaek
        error_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        path = error_dir / f"{step}_{ts}.png"
        await page.screenshot(path=str(path), full_page=True)
        logger.info("Screenshot saved: %s", path)
        return path
    except Exception as exc:
        logger.warning("Could not save screenshot: %s", exc)
        return None


async def get_element_text(page: Page, selector: SelectorSet) -> Optional[str]:
    all_selectors = [selector.primary] + selector.fallbacks
    for sel in all_selectors:
        try:
            el = await page.wait_for_selector(sel, timeout=5000)
            if el:
                return await el.inner_text()
        except Exception:
            continue
    return None


async def wait_for_download(page: Page, trigger_fn, download_dir: Path) -> Optional[Path]:
    """Trigger a download and wait for it to complete. Returns saved path."""
    async with page.expect_download(timeout=60000) as dl_info:
        await trigger_fn()
    download = await dl_info.value
    suggested = download.suggested_filename or "download.pdf"
    dest = download_dir / suggested
    await download.save_as(str(dest))
    return dest
