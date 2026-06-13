"""
TEE SSO login flow for engineers (μηχανικοί).

Entry point: ktimatologio.gov.gr/Professionals/Account/LoginEngineer
SSO: Oracle Access Manager (OAM) at sso.tee.gr — form fields: username, password
Post-login: ktimatologio.gov.gr/Professionals/ professional research portal
"""
import logging
from playwright.async_api import Page, BrowserContext, TimeoutError as PWTimeout

from config import Config
from automation.browser_manager import (
    human_delay, safe_click, safe_fill,
    screenshot_on_error, wait_for_any, page_has_text,
)
from automation.selectors import LOGIN, STATES, NAV
from database.db import add_log

logger = logging.getLogger(__name__)

# Engineer portal entry point — navigating here triggers SSO redirect
ENGINEER_LOGIN_URL = "https://ktimatologio.gov.gr/Professionals/Account/LoginEngineer"

# OAM SSO is at sso.tee.gr — detected by URL after redirect
OAM_HOST = "sso.tee.gr"

# Text visible on the professional portal after successful login
PORTAL_AFTER_LOGIN_INDICATORS = [
    "Εξουσιοδότηση",
    "Αποσύνδεση",
    "Αποσύνδεση",
    "Καλώς ήρθατε",
    "Logout",
    "Professionals",
    "Έρευνα",
    "Αναζήτηση",
]


class AuthError(Exception):
    pass


async def login(ctx: BrowserContext) -> Page:
    """
    Navigate to the engineer professional portal, handle OAM SSO redirect,
    submit TEE credentials, and return the authenticated page.
    Raises AuthError on failure.
    """
    if not Config.TEE_USERNAME or not Config.TEE_PASSWORD:
        raise AuthError("TEE_USERNAME or TEE_PASSWORD not set in environment")

    page = await ctx.new_page()
    add_log(None, "login", "info", f"Navigating to engineer portal: {ENGINEER_LOGIN_URL}")

    # Step 1: Navigate to engineer login entry point
    try:
        await page.goto(ENGINEER_LOGIN_URL, wait_until="domcontentloaded",
                        timeout=Config.PAGE_LOAD_TIMEOUT)
    except Exception as exc:
        add_log(None, "login", "failure", f"Cannot reach engineer portal: {exc}")
        raise AuthError(f"Cannot reach engineer portal: {exc}") from exc

    await human_delay(1.5, 3.0)

    # Step 2: Wait for OAM SSO redirect (sso.tee.gr)
    current_url = page.url
    add_log(None, "login", "info", f"After navigation: {current_url[:80]}")

    if OAM_HOST not in current_url:
        # May need to wait for redirect or click a login button
        try:
            await page.wait_for_url(f"**{OAM_HOST}**", timeout=10000)
        except PWTimeout:
            # Already on portal or redirected differently — try to continue
            add_log(None, "login", "warning",
                    f"No OAM redirect detected, at: {page.url[:80]}")

    await human_delay(0.5, 1.0)

    # Step 3: Fill TEE username and password on OAM login form
    username_ok = await safe_fill(page, LOGIN["username"], Config.TEE_USERNAME)
    if not username_ok:
        await screenshot_on_error(page, "_auth", "username_field_missing")
        raise AuthError(f"Username field not found. Current URL: {page.url[:80]}")

    await human_delay(0.3, 0.6)

    password_ok = await safe_fill(page, LOGIN["password"], Config.TEE_PASSWORD)
    if not password_ok:
        await screenshot_on_error(page, "_auth", "password_field_missing")
        raise AuthError("Password field not found on OAM login page")

    await human_delay(0.5, 1.0)

    # Step 4: Submit
    submit_ok = await safe_click(page, LOGIN["submit"])
    if not submit_ok:
        await screenshot_on_error(page, "_auth", "submit_missing")
        raise AuthError("Submit button not found on OAM login page")

    # Step 5: Wait for redirect back to professional portal
    try:
        await page.wait_for_load_state("networkidle", timeout=Config.PAGE_LOAD_TIMEOUT)
    except PWTimeout:
        pass

    await human_delay(2.0, 3.5)

    # Step 6: Check for OAM login errors (shown on same SSO page)
    for err_sel in [LOGIN["error"].primary] + LOGIN["error"].fallbacks:
        try:
            el = await page.query_selector(err_sel)
            if el and await el.is_visible():
                error_text = await el.inner_text()
                add_log(None, "login", "failure", f"OAM login error: {error_text}")
                raise AuthError(f"Login failed: {error_text}")
        except AuthError:
            raise
        except Exception:
            pass

    # Step 7: Verify we're on the professional portal
    final_url = page.url
    add_log(None, "login", "info", f"Post-login URL: {final_url[:80]}")

    found_indicator = await wait_for_any(page, PORTAL_AFTER_LOGIN_INDICATORS, timeout=12000)
    if not found_indicator:
        await screenshot_on_error(page, "_auth", "post_login_check")
        add_log(None, "login", "failure",
                f"No portal indicator found. URL: {final_url[:80]}")
        raise AuthError(
            f"Login may have failed — no portal indicator found. URL: {final_url[:80]}"
        )

    add_log(None, "login", "success", f"Authenticated at {final_url[:60]}")
    logger.info("TEE engineer login successful: %s", final_url[:60])
    return page


async def ensure_session_alive(page: Page, ctx: BrowserContext) -> Page:
    """
    Check if the session is still active. If expired, re-login and return a new page.
    """
    expired_indicators = [
        STATES["session_expired"].primary,
        *STATES["session_expired"].fallbacks,
    ]
    for sel in expired_indicators:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                logger.info("Session expired, re-authenticating")
                add_log(None, "session", "warning", "Session expired — re-authenticating")
                await page.close()
                return await login(ctx)
        except Exception:
            pass

    # Also check if we can still see the main nav
    nav_visible = await page_has_text(page, "Εξουσιοδότηση")
    if not nav_visible:
        try:
            await page.goto(
                "https://ktimatologio.gov.gr/Professionals/",
                wait_until="networkidle",
            )
            await human_delay(1.0, 2.0)
        except Exception:
            pass

    return page


async def navigate_to_kaek_search(page: Page) -> bool:
    """
    From the authenticated portal home, navigate through:
    Εξουσιοδότηση → Έρευνα → Ακίνητο

    Returns True if navigation succeeded.
    """
    add_log(None, "navigate", "info", "Navigating to Εξουσιοδότηση → Έρευνα → Ακίνητο")

    # Step 1: Click Εξουσιοδότηση
    ok = await safe_click(page, NAV["exousiodotisi"])
    if not ok:
        await screenshot_on_error(page, "_nav", "exousiodotisi_missing")
        add_log(None, "navigate", "failure", "Εξουσιοδότηση menu item not found")
        return False

    await human_delay(0.8, 1.5)

    # Step 2: Click Έρευνα
    ok = await safe_click(page, NAV["erevna"])
    if not ok:
        await screenshot_on_error(page, "_nav", "erevna_missing")
        add_log(None, "navigate", "failure", "Έρευνα menu item not found")
        return False

    await human_delay(0.8, 1.5)

    # Step 3: Click Ακίνητο
    ok = await safe_click(page, NAV["akinito"])
    if not ok:
        await screenshot_on_error(page, "_nav", "akinito_missing")
        add_log(None, "navigate", "failure", "Ακίνητο menu item not found")
        return False

    await human_delay(1.0, 2.0)
    try:
        await page.wait_for_load_state("networkidle", timeout=Config.PAGE_LOAD_TIMEOUT)
    except PWTimeout:
        pass

    add_log(None, "navigate", "success", "Reached KAEK search form")
    return True
