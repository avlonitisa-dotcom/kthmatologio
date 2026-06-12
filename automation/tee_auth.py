"""
TEE SSO login flow.

IMPORTANT: Update PORTAL_AFTER_LOGIN_INDICATORS once you have inspected
the live portal after a successful login.
"""
import logging
from typing import Optional

from playwright.async_api import Page, BrowserContext, TimeoutError as PWTimeout

from config import Config
from automation.browser_manager import (
    human_delay, safe_click, safe_fill,
    screenshot_on_error, wait_for_any, page_has_text,
)
from automation.selectors import LOGIN, STATES, NAV
from database.db import add_log

logger = logging.getLogger(__name__)

# Strings visible on the portal after a successful TEE login.
# Update these after manually inspecting the authenticated landing page.
PORTAL_AFTER_LOGIN_INDICATORS = [
    "Εξουσιοδότηση",
    "Αποσύνδεση",
    "Αποσύνδεση",
    "Καλώς ήρθατε",
    "Logout",
]


class AuthError(Exception):
    pass


async def login(ctx: BrowserContext) -> Page:
    """
    Open a new page, navigate to TEE SSO, submit credentials, and return
    the authenticated page ready for navigation.
    Raises AuthError on failure.
    """
    if not Config.TEE_USERNAME or not Config.TEE_PASSWORD:
        raise AuthError("TEE_USERNAME or TEE_PASSWORD not set in .env")

    page = await ctx.new_page()
    add_log(None, "login", "info", "Navigating to TEE SSO login page")

    try:
        await page.goto(Config.TEE_SSO_URL, wait_until="networkidle")
    except Exception as exc:
        add_log(None, "login", "failure", f"Cannot reach SSO URL: {exc}")
        raise AuthError(f"Cannot reach SSO URL: {exc}") from exc

    await human_delay(1.0, 2.0)

    # Fill username
    username_ok = await safe_fill(page, LOGIN["username"], Config.TEE_USERNAME)
    if not username_ok:
        await screenshot_on_error(page, "_auth", "username_field_missing")
        raise AuthError("Username input field not found on login page")

    await human_delay(0.3, 0.8)

    # Fill password
    password_ok = await safe_fill(page, LOGIN["password"], Config.TEE_PASSWORD)
    if not password_ok:
        await screenshot_on_error(page, "_auth", "password_field_missing")
        raise AuthError("Password input field not found on login page")

    await human_delay(0.5, 1.2)

    # Submit
    submit_ok = await safe_click(page, LOGIN["submit"])
    if not submit_ok:
        await screenshot_on_error(page, "_auth", "submit_missing")
        raise AuthError("Submit button not found on login page")

    # Wait for navigation
    try:
        await page.wait_for_load_state("networkidle", timeout=Config.PAGE_LOAD_TIMEOUT)
    except PWTimeout:
        pass  # Some portals don't reach networkidle cleanly

    await human_delay(1.5, 3.0)

    # Check for login errors
    error_el = await page.query_selector(LOGIN["error"].primary)
    if error_el:
        error_text = await error_el.inner_text()
        add_log(None, "login", "failure", f"Login error shown: {error_text}")
        raise AuthError(f"Login failed: {error_text}")

    # Verify we're authenticated
    found_indicator = await wait_for_any(
        page,
        PORTAL_AFTER_LOGIN_INDICATORS,
        timeout=10000,
    )
    if not found_indicator:
        await screenshot_on_error(page, "_auth", "post_login_check")
        add_log(None, "login", "failure", "No authenticated portal indicator found after login")
        raise AuthError("Login did not redirect to authenticated portal")

    add_log(None, "login", "success", "Authenticated successfully")
    logger.info("TEE SSO login successful")
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
        # Could be on a redirect; navigate back to portal
        try:
            await page.goto(Config.KTIMATOLOGIO_PORTAL_URL, wait_until="networkidle")
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
