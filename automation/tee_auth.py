"""
TEE SSO login flow for engineers (μηχανικοί).

Strategy: plain HTTP (httpx) handles the entire OAuth/OAM handshake — bypasses
the WAF at ktimatologio.gov.gr that blocks headless browsers.  Once session
cookies are obtained, they are injected into the Playwright context and the
browser navigates directly to the portal home.

Flow:
  1. httpx GET ktimatologio.gov.gr/Professionals/Account/LoginTee
     → follows 302 chain → services.tee.gr/extauthws → sso.tee.gr OAM page
  2. httpx POST sso.tee.gr/oam/server/auth_cred_submit (username + password
     + hidden fields scraped from the OAM form)
     → follows 302 chain back to ktimatologio.gov.gr → session cookie set
  3. Inject all cookies into Playwright BrowserContext
  4. Playwright navigates to portal home (session already live)
"""
import logging
import re
from typing import Optional

import httpx
from playwright.async_api import Page, BrowserContext, TimeoutError as PWTimeout

from config import Config
from automation.browser_manager import (
    human_delay, safe_click,
    screenshot_on_error, page_has_text,
)
from automation.selectors import STATES, NAV
from database.db import add_log

logger = logging.getLogger(__name__)

# Triggers the OAuth/OAM redirect chain (the "Είσοδος στην Υπηρεσία" button)
TEE_LOGIN_URL = "https://ktimatologio.gov.gr/Professionals/Account/LoginTee"
# OAM credential submission endpoint (on sso.tee.gr — not WAF-protected)
OAM_SUBMIT_URL = "https://sso.tee.gr/oam/server/auth_cred_submit"
# Professional portal home
PORTAL_HOME = "https://ktimatologio.gov.gr/Professionals/"

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "el-GR,el;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Playwright text selectors that confirm an active portal session
_PORTAL_INDICATORS = [
    "text=Εξουσιοδότηση",
    "text=Αποσύνδεση",
    "text=Καλώς ήρθατε",
    "text=Logout",
    "text=Έρευνα",
    "text=Αναζήτηση",
]


class AuthError(Exception):
    pass


async def _sso_via_httpx() -> tuple[list[dict], str]:
    """
    Follow the entire TEE SSO redirect chain with plain HTTP and submit
    credentials to OAM.  Returns (playwright_cookies, final_url).
    """
    add_log(None, "login", "info", "httpx SSO: starting redirect chain")

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers=_HTTP_HEADERS,
        timeout=30.0,
    ) as client:
        # Step 1: GET LoginTee → services.tee.gr → sso.tee.gr OAM form
        r = await client.get(TEE_LOGIN_URL)
        oam_url = str(r.url)
        html = r.text
        add_log(None, "login", "info", f"OAM URL: {oam_url[:100]}")

        if "sso.tee.gr" not in oam_url:
            add_log(None, "login", "failure", f"No OAM redirect, ended at: {oam_url[:100]}")
            raise AuthError(f"Expected sso.tee.gr OAM page, got: {oam_url[:100]}")

        # Step 2: build POST payload (credentials + hidden OAM fields)
        post_data: dict[str, str] = {
            "username": Config.TEE_USERNAME,
            "password": Config.TEE_PASSWORD,
        }
        for m in re.finditer(
            r'<input[^>]+type=["\']hidden["\'][^>]*/?>',
            html,
            re.IGNORECASE,
        ):
            tag = m.group(0)
            name_m = re.search(r'\bname=["\']([^"\']+)["\']', tag)
            val_m  = re.search(r'\bvalue=["\']([^"\']*)["\']', tag)
            if name_m:
                post_data[name_m.group(1)] = val_m.group(1) if val_m else ""

        hidden_keys = [k for k in post_data if k not in ("username", "password")]
        add_log(None, "login", "info", f"OAM hidden fields: {hidden_keys}")

        # Step 3: submit credentials
        r2 = await client.post(OAM_SUBMIT_URL, data=post_data)
        final_url = str(r2.url)
        add_log(None, "login", "info", f"Post-submit URL: {final_url[:100]}")

        # Login failed → OAM did not redirect away from sso.tee.gr
        if "sso.tee.gr" in final_url:
            raise AuthError(
                "OAM rejected credentials (still on sso.tee.gr). "
                "Check TEE_USERNAME / TEE_PASSWORD."
            )

        # Step 4: collect cookies and convert to Playwright format
        pw_cookies: list[dict] = []
        seen: set[str] = set()
        for cookie in client.cookies.jar:
            key = f"{cookie.domain}:{cookie.name}"
            if key in seen:
                continue
            seen.add(key)
            domain = cookie.domain or "ktimatologio.gov.gr"
            if not domain.startswith("."):
                domain = f".{domain}"
            pw_cookies.append({
                "name":   cookie.name,
                "value":  cookie.value,
                "domain": domain,
                "path":   cookie.path or "/",
            })

        add_log(
            None, "login", "info",
            f"Cookies: {[c['name'] for c in pw_cookies]} at {final_url[:60]}",
        )
        return pw_cookies, final_url


async def login(ctx: BrowserContext) -> Page:
    """
    Authenticate via TEE SSO, inject session into Playwright, and return
    the authenticated page on the professional portal.
    Raises AuthError on failure.
    """
    if not Config.TEE_USERNAME or not Config.TEE_PASSWORD:
        raise AuthError("TEE_USERNAME or TEE_PASSWORD not set in environment")

    # Phase 1: full SSO via plain HTTP (bypasses WAF entirely)
    pw_cookies, _ = await _sso_via_httpx()

    # Phase 2: inject session cookies into Playwright context
    if pw_cookies:
        await ctx.add_cookies(pw_cookies)
        add_log(None, "login", "info",
                f"Injected {len(pw_cookies)} cookies into browser context")

    # Phase 3: navigate Playwright to portal home (session already live)
    page = await ctx.new_page()
    add_log(None, "login", "info", f"Playwright → {PORTAL_HOME}")
    try:
        await page.goto(PORTAL_HOME, wait_until="domcontentloaded",
                        timeout=Config.PAGE_LOAD_TIMEOUT)
    except Exception as exc:
        add_log(None, "login", "failure", f"Cannot reach portal: {exc}")
        raise AuthError(f"Cannot reach portal after SSO: {exc}") from exc

    await human_delay(1.5, 3.0)

    # Phase 4: verify session is active
    found: Optional[str] = None
    for sel in _PORTAL_INDICATORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                found = sel
                break
        except Exception:
            pass

    if not found:
        await screenshot_on_error(page, "_auth", "post_sso_portal")
        current_url = page.url
        add_log(None, "login", "failure",
                f"Portal indicators not found. URL: {current_url[:80]}")
        raise AuthError(
            f"Portal session not established after SSO. URL: {current_url[:80]}"
        )

    add_log(None, "login", "success", f"Authenticated at {page.url[:60]}")
    logger.info("TEE engineer login successful: %s", page.url[:60])
    return page


async def ensure_session_alive(page: Page, ctx: BrowserContext) -> Page:
    """Check if session is active; re-login if expired."""
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

    nav_visible = await page_has_text(page, "Εξουσιοδότηση")
    if not nav_visible:
        try:
            await page.goto(PORTAL_HOME, wait_until="networkidle")
            await human_delay(1.0, 2.0)
        except Exception:
            pass

    return page


async def navigate_to_kaek_search(page: Page) -> bool:
    """
    Navigate authenticated portal: Εξουσιοδότηση → Έρευνα → Ακίνητο.
    Returns True on success.
    """
    add_log(None, "navigate", "info", "Navigating Εξουσιοδότηση → Έρευνα → Ακίνητο")

    ok = await safe_click(page, NAV["exousiodotisi"])
    if not ok:
        await screenshot_on_error(page, "_nav", "exousiodotisi_missing")
        add_log(None, "navigate", "failure", "Εξουσιοδότηση not found")
        return False
    await human_delay(0.8, 1.5)

    ok = await safe_click(page, NAV["erevna"])
    if not ok:
        await screenshot_on_error(page, "_nav", "erevna_missing")
        add_log(None, "navigate", "failure", "Έρευνα not found")
        return False
    await human_delay(0.8, 1.5)

    ok = await safe_click(page, NAV["akinito"])
    if not ok:
        await screenshot_on_error(page, "_nav", "akinito_missing")
        add_log(None, "navigate", "failure", "Ακίνητο not found")
        return False
    await human_delay(1.0, 2.0)

    try:
        await page.wait_for_load_state("networkidle", timeout=Config.PAGE_LOAD_TIMEOUT)
    except PWTimeout:
        pass

    add_log(None, "navigate", "success", "Reached KAEK search form")
    return True
