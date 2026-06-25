"""
TEE SSO login flow for engineers (μηχανικοί).

All portal interactions use plain httpx — the WAF at ktimatologio.gov.gr
blocks Playwright/headless browsers but not regular HTTP requests.

Flow:
  1. httpx GET ktimatologio.gov.gr/Professionals/Account/LoginTee
     → follows 302 chain → services.tee.gr → sso.tee.gr OAM login form
  2. httpx POST sso.tee.gr/oam/server/auth_cred_submit
     → follows 302 chain back to ktimatologio.gov.gr → session cookies set
  3. Session cookies stored in _portal_cookies (module-level)
  4. All subsequent portal requests use make_portal_client() which creates
     a fresh httpx.AsyncClient pre-loaded with those cookies
"""
import logging
import re
from typing import Optional

import httpx

from config import Config
from database.db import add_log

logger = logging.getLogger(__name__)

TEE_LOGIN_URL  = "https://ktimatologio.gov.gr/Professionals/Account/LoginTee"
OAM_SUBMIT_URL = "https://sso.tee.gr/oam/server/auth_cred_submit"
PORTAL_HOME    = "https://ktimatologio.gov.gr/Professionals/"
KAEK_SEARCH_URL = "https://ktimatologio.gov.gr/Professionals/Inquiry/Main/SearchByEstate"

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

# Module-level session state — populated by login()
_portal_cookies: dict[str, str] = {}
_logged_in: bool = False


class AuthError(Exception):
    pass


def make_portal_client() -> httpx.AsyncClient:
    """
    Return a new httpx.AsyncClient pre-loaded with the current portal session
    cookies.  Raises AuthError if login() has not been called yet.
    """
    if not _portal_cookies:
        raise AuthError("Not logged in — call login() first")
    return httpx.AsyncClient(
        cookies=_portal_cookies,
        headers=_HTTP_HEADERS,
        follow_redirects=True,
        timeout=40.0,
    )


def is_logged_in() -> bool:
    return _logged_in and bool(_portal_cookies)


async def login() -> None:
    """
    Authenticate via TEE SSO using plain HTTP (bypasses WAF).
    Stores session cookies in _portal_cookies for reuse.
    Raises AuthError on failure.
    """
    global _portal_cookies, _logged_in

    if not Config.TEE_USERNAME or not Config.TEE_PASSWORD:
        raise AuthError("TEE_USERNAME or TEE_PASSWORD not set in environment")

    add_log(None, "login", "info", "httpx SSO: starting redirect chain")

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers=_HTTP_HEADERS,
        timeout=30.0,
    ) as client:
        # Step 1: GET LoginTee → follows chain to sso.tee.gr OAM form
        r = await client.get(TEE_LOGIN_URL)
        oam_url = str(r.url)
        html = r.text
        add_log(None, "login", "info", f"OAM URL: {oam_url[:100]}")

        if "sso.tee.gr" not in oam_url:
            add_log(None, "login", "failure", f"No OAM redirect, ended at: {oam_url[:100]}")
            raise AuthError(f"Expected sso.tee.gr OAM page, got: {oam_url[:100]}")

        # Step 2: build POST payload (credentials + all hidden fields from form)
        post_data: dict[str, str] = {
            "username": Config.TEE_USERNAME,
            "password": Config.TEE_PASSWORD,
        }
        for m in re.finditer(r'<input[^>]+type=["\']hidden["\'][^>]*/?>',
                              html, re.IGNORECASE):
            tag = m.group(0)
            name_m = re.search(r'\bname=["\']([^"\']+)["\']', tag)
            val_m  = re.search(r'\bvalue=["\']([^"\']*)["\']', tag)
            if name_m:
                post_data[name_m.group(1)] = val_m.group(1) if val_m else ""

        hidden_keys = [k for k in post_data if k not in ("username", "password")]
        add_log(None, "login", "info", f"OAM hidden fields: {hidden_keys}")

        # Extract form action URL from HTML
        action_m = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if action_m:
            raw = action_m.group(1)
            submit_url = (raw if raw.startswith("http")
                          else f"https://sso.tee.gr{raw}" if raw.startswith("/")
                          else f"https://sso.tee.gr/oam/server/{raw}")
        else:
            submit_url = OAM_SUBMIT_URL
        add_log(None, "login", "info", f"Form action: {submit_url}")

        # Step 3: submit credentials
        r2 = await client.post(submit_url, data=post_data)
        final_url = str(r2.url)
        add_log(None, "login", "info",
                f"Post-submit URL: {final_url[:100]} (status={r2.status_code})")

        if "sso.tee.gr" in final_url:
            err_snippet = ""
            for pat in [
                r'<p[^>]+class="[^"]*error[^"]*"[^>]*>([^<]{5,200})</p>',
                r'<span[^>]+class="[^"]*error[^"]*"[^>]*>([^<]{5,200})</span>',
            ]:
                m = re.search(pat, r2.text, re.IGNORECASE)
                if m:
                    err_snippet = m.group(1).strip()
                    break
            add_log(None, "login", "failure",
                    f"OAM error: '{err_snippet or r2.text[200:400].strip()}'")
            raise AuthError(
                "OAM rejected credentials — verify TEE_USERNAME / TEE_PASSWORD."
                + (f" OAM says: {err_snippet}" if err_snippet else "")
            )

        # Step 4: collect all cookies from the session
        cookies: dict[str, str] = {}
        for cookie in client.cookies.jar:
            cookies[cookie.name] = cookie.value

        add_log(None, "login", "info",
                f"Session cookies: {list(cookies.keys())} at {final_url[:60]}")

    _portal_cookies = cookies
    _logged_in = True
    logger.info("TEE login successful, cookies: %s", list(cookies.keys()))


async def ensure_logged_in() -> None:
    """Re-login if the session has expired or was never established."""
    if not is_logged_in():
        await login()
        return

    # Quick session check: GET portal home, see if we're redirected to login
    try:
        async with make_portal_client() as client:
            r = await client.get(PORTAL_HOME)
            if "Login" in str(r.url) or "sso.tee.gr" in str(r.url):
                add_log(None, "session", "warning",
                        f"Session expired (redirected to {str(r.url)[:60]}) — re-logging in")
                global _portal_cookies, _logged_in
                _portal_cookies = {}
                _logged_in = False
                await login()
    except Exception as exc:
        logger.warning("Session check failed: %s", exc)
        await login()


# ── Legacy Playwright helpers (kept for backward compat but not used for WAF pages) ──

async def login_playwright(ctx) -> object:
    """
    Legacy: authenticate and return a Playwright page.
    Now just calls login() (httpx) and returns a plain page on portal home.
    The page will be WAF-blocked for portal URLs but is kept for compatibility.
    """
    await login()
    from playwright.async_api import BrowserContext
    page = await ctx.new_page()
    try:
        await page.goto(PORTAL_HOME, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass
    return page


async def navigate_to_kaek_search(page) -> bool:
    """
    Legacy: now always returns False — use tee_search.py httpx approach instead.
    Kept so kaek_workflow.py callers can detect the change gracefully.
    """
    add_log(None, "navigate", "info",
            "navigate_to_kaek_search is deprecated — using httpx search instead")
    return False
