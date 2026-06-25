"""
httpx-based KAEK search and PDF download for ktimatologio.gov.gr.

The search form at SearchByEstate has THREE separate input fields separated by /
visual separators: [base_kaek] / [building_no] / [floor_no]
e.g. "050695715003" / "" / "" → searches all sub-units
     "050695715003" / "0" / "0" → searches only the land parcel /0/0

After search, a results table appears. Each row has a 🔍 (magnifier) at the right
that links to the detail page (Προβολή Εγγράφων Ακινήτου). That page has:
  ΑΠΟΣΠΑΣΜΑ ΠΕΡΙΓΡΑΦΙΚΗΣ ΒΑΣΗΣ   ΑΠΟΣΠΑΣΜΑ ΧΩΡΙΚΗΣ ΒΑΣΗΣ   ΕΜΦΑΝΙΣΗ ΣΤΟΝ ΧΑΡΤΗ

We target the /0/0 sub-unit (the land parcel itself).
"""
import logging
import re
from pathlib import Path
from typing import Optional

import httpx

from automation.tee_auth import make_portal_client, ensure_logged_in, KAEK_SEARCH_URL
from config import Config
from database.db import add_log

logger = logging.getLogger(__name__)

BASE_URL = "https://ktimatologio.gov.gr"


# ── Helpers ────────────────────────────────────────────────────────────────────

def split_kaek(kaek: str) -> tuple[str, str, str]:
    """
    Split a KAEK string into (base, building, floor).
    '050695715003'         → ('050695715003', '0', '0')
    '050695715003/0/0'     → ('050695715003', '0', '0')
    '050695715003/1/0'     → ('050695715003', '1', '0')
    """
    parts = kaek.strip().split("/")
    base = parts[0].strip()
    bld  = parts[1].strip() if len(parts) > 1 else "0"
    flr  = parts[2].strip() if len(parts) > 2 else "0"
    return base, bld, flr


def kaek_to_portal(kaek: str) -> str:
    """DB format 050695715003/0/0 → portal hyphen format 050695715003-0-0."""
    return kaek.strip().replace("/", "-")


def _extract_csrf(html: str) -> str:
    for pat in [
        r'<input[^>]+name=["\']__RequestVerificationToken["\'][^>]+value=["\']([^"\']+)["\']',
        r'<input[^>]+value=["\']([^"\']+)["\'][^>]+name=["\']__RequestVerificationToken["\']',
        r'<meta[^>]+name=["\']__RequestVerificationToken["\'][^>]+content=["\']([^"\']+)["\']',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


def _extract_form_action(html: str, fallback: str) -> str:
    m = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        raw = m.group(1)
        return raw if raw.startswith("http") else BASE_URL + raw
    return fallback


def _find_kaek_fields(html: str) -> list[str]:
    """
    Discover the name attributes of the 3 KAEK sub-fields.
    Returns [base_field_name, building_field_name, floor_field_name].
    Falls back to common ASP.NET MVC names.
    """
    # Look for 3+ text/number inputs in sequence
    inputs = re.findall(
        r'<input[^>]+type=["\'](?:text|number)["\'][^>]*>',
        html, re.IGNORECASE
    )
    names = []
    for inp in inputs:
        m = re.search(r'\bname=["\']([^"\']+)["\']', inp)
        if m:
            names.append(m.group(1))
        if len(names) == 3:
            break

    if len(names) >= 3:
        return names[:3]

    # Fallbacks: common ASP.NET MVC property names for this portal
    return ["Kaek", "BuildingNumber", "FloorNumber"]


def _find_detail_url(html: str, kaek_base: str, bld: str, flr: str) -> Optional[str]:
    """
    Find the magnifier (🔍) link for the target sub-unit in the results table.
    Prefers the /0/0 land-parcel row; falls back to the first result row.
    """
    # Look for any link near the specific KAEK sub-unit string
    # Portal shows KAEK as "050695715003/0/ 0" or "050695715003/0/0" in the table
    target_variants = [
        f"{kaek_base}/{bld}/ {flr}",
        f"{kaek_base}/{bld}/{flr}",
        f"{kaek_base}-{bld}-{flr}",
    ]
    for variant in target_variants:
        # Find a <td> containing this text, then look for nearby <a> with a href
        row_pat = (
            rf'<tr[^>]*>(?:(?!<tr).)*?'
            rf'{re.escape(variant)}'
            rf'(?:(?!<tr).)*?'
            rf'<a[^>]+href=["\']([^"\']+)["\']'
        )
        m = re.search(row_pat, html, re.IGNORECASE | re.DOTALL)
        if m:
            url = m.group(1)
            return url if url.startswith("http") else BASE_URL + url

    # Fallback: first magnifier / detail link in the page
    for pat in [
        r'href=["\']([^"\']*[Ss]earch[Bb]y[Ee]state[^"\']*)["\']',
        r'href=["\']([^"\']*[Pp]ro[bv]ol[^"\']*)["\']',
        r'href=["\']([^"\']*[Ee]ggrafa[^"\']*)["\']',
        r'href=["\']([^"\']*[Dd]etail[^"\']*)["\']',
        # Generic: any <a> with a href containing the base KAEK
        rf'href=["\']([^"\']*{re.escape(kaek_base)}[^"\']*)["\']',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            url = m.group(1)
            return url if url.startswith("http") else BASE_URL + url

    return None


def _find_pdf_action(html: str, pdf_type: str) -> Optional[dict]:
    """
    Find the form/link action for a PDF download button.
    Returns {"url": ..., "method": "GET"|"POST", "fields": {...}} or None.
    """
    keywords = {
        "perigrafiki": ["ΠΕΡΙΓΡΑΦΙΚΗΣ", "Perigrafiki", "perigrafiki"],
        "xoriki":      ["ΧΩΡΙΚΗΣ",      "Xoriki",      "xoriki"],
    }

    for kw in keywords.get(pdf_type, []):
        # <a href="...">...keyword...</a>
        m = re.search(
            rf'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(?:[^<]|<(?!/a>))*{re.escape(kw)}',
            html, re.IGNORECASE | re.DOTALL
        )
        if m:
            url = m.group(1)
            return {"url": url if url.startswith("http") else BASE_URL + url,
                    "method": "GET", "fields": {}}

        # <form> that contains the keyword (button submits a form)
        for fm in re.finditer(r'<form[^>]*>.*?</form>', html, re.IGNORECASE | re.DOTALL):
            if kw.lower() not in fm.group(0).lower():
                continue
            act_m = re.search(r'action=["\']([^"\']+)["\']', fm.group(0), re.IGNORECASE)
            action = act_m.group(1) if act_m else ""
            if not action.startswith("http"):
                action = BASE_URL + action
            fields: dict[str, str] = {}
            for inp_m in re.finditer(
                r'<input[^>]+type=["\']hidden["\'][^>]*/?>',
                fm.group(0), re.IGNORECASE
            ):
                nm = re.search(r'\bname=["\']([^"\']+)["\']', inp_m.group(0))
                vm = re.search(r'\bvalue=["\']([^"\']*)["\']', inp_m.group(0))
                if nm:
                    fields[nm.group(1)] = vm.group(1) if vm else ""
            return {"url": action, "method": "POST", "fields": fields}

    return None


async def _download_pdf(
    client: httpx.AsyncClient,
    action: dict,
    dest_path: Path,
    kaek: str,
    label: str,
) -> bool:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if action["method"] == "GET":
            r = await client.get(action["url"])
        else:
            r = await client.post(action["url"], data=action["fields"])

        ct = r.headers.get("content-type", "")
        add_log(kaek, f"dl_{label}", "info",
                f"status={r.status_code} ct={ct[:40]} size={len(r.content)}")

        if r.status_code == 200 and ("pdf" in ct or "octet-stream" in ct
                                      or len(r.content) > 10_000):
            dest_path.write_bytes(r.content)
            add_log(kaek, f"dl_{label}", "success",
                    f"Saved {len(r.content)} bytes → {dest_path.name}")
            return True

        add_log(kaek, f"dl_{label}", "failure",
                f"Unexpected response: {r.status_code} {ct[:40]} body={r.text[:300]}")
        return False
    except Exception as exc:
        add_log(kaek, f"dl_{label}", "failure", str(exc))
        return False


# ── Main entry point ───────────────────────────────────────────────────────────

async def search_and_download(
    kaek: str,
    dest_dir: Optional[Path] = None,
) -> dict:
    """
    Search for a KAEK on the professional portal and download both PDFs.
    Uses the 3-field KAEK form: [base] / [building] / [floor].
    For land parcels, targets the /0/0 sub-unit.

    Returns dict with: success, perigrafiki_ok, xoriki_ok,
                        pdf_perigrafiki_path, pdf_xoriki_path, failure_reason
    """
    if dest_dir is None:
        dest_dir = Config.DOWNLOADS_DIR / kaek.replace("/", "_")

    result = {
        "kaek": kaek,
        "success": False,
        "perigrafiki_ok": False,
        "xoriki_ok": False,
        "pdf_perigrafiki_path": None,
        "pdf_xoriki_path": None,
        "failure_reason": None,
    }

    await ensure_logged_in()

    base, bld, flr = split_kaek(kaek)
    kaek_safe = kaek.replace("/", "_")

    async with make_portal_client() as client:
        # ── Step 1: GET search form ────────────────────────────────────────────
        r_form = await client.get(KAEK_SEARCH_URL)
        add_log(kaek, "search", "info",
                f"GET form: status={r_form.status_code} url={str(r_form.url)[:80]}")

        if r_form.status_code != 200:
            result["failure_reason"] = f"Search form returned HTTP {r_form.status_code}"
            add_log(kaek, "search", "failure", result["failure_reason"])
            return result

        # Log form HTML to help identify exact field names on first KAEK
        add_log(kaek, "form_html", "info",
                f"(first 2000 chars) {r_form.text[:2000]}")

        csrf       = _extract_csrf(r_form.text)
        form_action = _extract_form_action(r_form.text, KAEK_SEARCH_URL)
        fields      = _find_kaek_fields(r_form.text)
        f_base, f_bld, f_flr = fields[0], fields[1], fields[2]

        add_log(kaek, "search", "info",
                f"form_action={form_action[:60]} fields={fields} "
                f"csrf={'yes' if csrf else 'no'}")

        # ── Step 2: POST search with KAEK ─────────────────────────────────────
        # Fill all 3 sub-fields; leave building/floor as the split values
        post_data: dict[str, str] = {
            f_base: base,
            f_bld:  bld,
            f_flr:  flr,
        }
        if csrf:
            post_data["__RequestVerificationToken"] = csrf

        r2 = await client.post(form_action, data=post_data)
        add_log(kaek, "search", "info",
                f"POST: status={r2.status_code} url={str(r2.url)[:80]}")
        add_log(kaek, "results_html", "info",
                f"(first 2000 chars) {r2.text[:2000]}")

        # ── Step 3: navigate to detail page ───────────────────────────────────
        # Check if POST redirected directly to detail page
        r2_url = str(r2.url)
        if "Login" in r2_url or "sso.tee.gr" in r2_url:
            add_log(kaek, "search", "warning",
                    "Session expired mid-search — re-logging in")
            from automation.tee_auth import login as tee_login
            await tee_login()
            # Retry once
            async with make_portal_client() as c2:
                r2 = await c2.post(form_action, data=post_data)

        is_detail = any(kw in r2_url.lower() for kw in
                        ["provolh", "provohi", "documents", "eggrafa",
                         "estate", "property", "akinito"])
        if is_detail:
            detail_html = r2.text
            detail_final_url = r2_url
        else:
            # Find the magnifier link for our target sub-unit
            detail_link = _find_detail_url(r2.text, base, bld, flr)
            if not detail_link:
                add_log(kaek, "search", "failure",
                        f"No detail link for {base}/{bld}/{flr} in results")
                result["failure_reason"] = (
                    f"Magnifier link not found in results for {base}/{bld}/{flr}"
                )
                return result

            r3 = await client.get(detail_link)
            add_log(kaek, "detail", "info",
                    f"Detail: status={r3.status_code} url={str(r3.url)[:80]}")
            add_log(kaek, "detail_html", "info",
                    f"(first 2000 chars) {r3.text[:2000]}")
            detail_html = r3.text
            detail_final_url = str(r3.url)

        # ── Step 4: find PDF button actions ───────────────────────────────────
        p_action = _find_pdf_action(detail_html, "perigrafiki")
        x_action = _find_pdf_action(detail_html, "xoriki")
        add_log(kaek, "pdf_actions", "info",
                f"perigrafiki={p_action} xoriki={x_action}")

        if not p_action and not x_action:
            result["failure_reason"] = (
                f"PDF buttons not found on {detail_final_url[:80]}"
            )
            add_log(kaek, "detail", "failure", result["failure_reason"])
            return result

        # ── Step 5: download PDFs ──────────────────────────────────────────────
        dest_dir.mkdir(parents=True, exist_ok=True)

        if p_action:
            dest_p = dest_dir / f"{kaek_safe}_perigrafiki_vasi.pdf"
            ok = await _download_pdf(client, p_action, dest_p, kaek, "perigrafiki")
            result["perigrafiki_ok"] = ok
            if ok:
                result["pdf_perigrafiki_path"] = str(dest_p)

        if x_action:
            dest_x = dest_dir / f"{kaek_safe}_xoriki_vasi.pdf"
            ok = await _download_pdf(client, x_action, dest_x, kaek, "xoriki")
            result["xoriki_ok"] = ok
            if ok:
                result["pdf_xoriki_path"] = str(dest_x)

    if result["perigrafiki_ok"] and result["xoriki_ok"]:
        result["success"] = True
    elif result["perigrafiki_ok"] or result["xoriki_ok"]:
        result["success"] = True
        result["failure_reason"] = "One PDF missing"

    return result
