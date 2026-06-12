"""
Centralized selector definitions for the TEE / Ktimatologio portal.

IMPORTANT: These selectors are best-effort placeholders based on common Greek
government portal patterns. After manually inspecting the live portal with
browser DevTools, update the PRIMARY selectors and keep the FALLBACK list
as alternatives.

Selector strategy (in priority order):
  1. Greek visible text (getByText / getByRole with name)
  2. ARIA labels in Greek
  3. name / id attributes
  4. CSS class / data-* (least stable — update when portal changes)
"""

from dataclasses import dataclass, field


@dataclass
class SelectorSet:
    """Primary selector plus ordered fallbacks."""
    primary: str
    fallbacks: list[str] = field(default_factory=list)
    description: str = ""


# ── Login Page ─────────────────────────────────────────────────────────────────
LOGIN = {
    "username": SelectorSet(
        primary='input[name="username"]',
        fallbacks=['input[id="username"]', 'input[type="text"]'],
        description="Username / ΑΜ ΤΕΕ input field",
    ),
    "password": SelectorSet(
        primary='input[name="password"]',
        fallbacks=['input[id="password"]', 'input[type="password"]'],
        description="Password input field",
    ),
    "submit": SelectorSet(
        primary='input[type="submit"]',
        fallbacks=[
            'button[type="submit"]',
            "button:has-text('Είσοδος')",
            "button:has-text('Login')",
            'input[value="Είσοδος"]',
        ],
        description="Login submit button",
    ),
    "error": SelectorSet(
        primary=".alert-error",
        fallbacks=[".error-message", "[class*='error']", ".kc-feedback-text"],
        description="Login error message container",
    ),
}

# ── Main Portal Navigation ─────────────────────────────────────────────────────
NAV = {
    "exousiodotisi": SelectorSet(
        primary="text='Εξουσιοδότηση'",
        fallbacks=[
            "a:has-text('Εξουσιοδότηση')",
            "span:has-text('Εξουσιοδότηση')",
            "li:has-text('Εξουσιοδότηση')",
        ],
        description="Εξουσιοδότηση menu item",
    ),
    "erevna": SelectorSet(
        primary="text='Έρευνα'",
        fallbacks=[
            "a:has-text('Έρευνα')",
            "span:has-text('Έρευνα')",
            "li:has-text('Έρευνα')",
        ],
        description="Έρευνα sub-menu item",
    ),
    "akinito": SelectorSet(
        primary="text='Ακίνητο'",
        fallbacks=[
            "a:has-text('Ακίνητο')",
            "span:has-text('Ακίνητο')",
            "li:has-text('Ακίνητο')",
        ],
        description="Ακίνητο menu item for KAEK search",
    ),
}

# ── KAEK Search Form ───────────────────────────────────────────────────────────
SEARCH = {
    "kaek_input": SelectorSet(
        primary='input[name="kaek"]',
        fallbacks=[
            'input[placeholder*="ΚΑΕΚ"]',
            'input[placeholder*="kaek"]',
            'input[id*="kaek"]',
            'input[type="text"]:first-of-type',
        ],
        description="KAEK input field in search form",
    ),
    "submit": SelectorSet(
        primary="button:has-text('Αναζήτηση')",
        fallbacks=[
            "input[type='submit']",
            "button[type='submit']",
            "button:has-text('Αναζήτηση')",
            "a:has-text('Αναζήτηση')",
        ],
        description="Search submit button",
    ),
    "results_table": SelectorSet(
        primary="table.results",
        fallbacks=[
            "table[class*='result']",
            ".search-results table",
            "table:has(td)",
        ],
        description="Results table after KAEK search",
    ),
    "result_row": SelectorSet(
        primary="table.results tbody tr:first-child",
        fallbacks=[
            ".search-results tr:first-child",
            "tbody tr:first-child",
        ],
        description="First result row",
    ),
    "magnifier_icon": SelectorSet(
        primary="img[src*='magnif']",
        fallbacks=[
            "a[title*='Ανάλυση']",
            "a[title*='Λεπτομέρεια']",
            "td a img",
            ".search-icon",
            "button:has-text('🔍')",
            "a.detail-link",
        ],
        description="Magnifying glass / detail icon in results row",
    ),
}

# ── PDF Download Section ───────────────────────────────────────────────────────
PDF = {
    "perigrafiki_link": SelectorSet(
        primary="text='ΑΠΟΣΠΑΣΜΑ ΠΕΡΙΓΡΑΦΙΚΗΣ ΒΑΣΗΣ'",
        fallbacks=[
            "a:has-text('ΠΕΡΙΓΡΑΦΙΚΗΣ ΒΑΣΗΣ')",
            "a:has-text('Περιγραφικής')",
            "a[href*='perigrafiki']",
            "a[href*='descriptive']",
        ],
        description="Link to download ΑΠΟΣΠΑΣΜΑ ΠΕΡΙΓΡΑΦΙΚΗΣ ΒΑΣΗΣ PDF",
    ),
    "xoriki_link": SelectorSet(
        primary="text='ΑΠΟΣΠΑΣΜΑ ΧΩΡΙΚΗΣ ΒΑΣΗΣ'",
        fallbacks=[
            "a:has-text('ΧΩΡΙΚΗΣ ΒΑΣΗΣ')",
            "a:has-text('Χωρικής')",
            "a[href*='xoriki']",
            "a[href*='spatial']",
        ],
        description="Link to download ΑΠΟΣΠΑΣΜΑ ΧΩΡΙΚΗΣ ΒΑΣΗΣ PDF",
    ),
    "download_button": SelectorSet(
        primary="button:has-text('Λήψη')",
        fallbacks=[
            "a:has-text('Λήψη')",
            "button:has-text('Download')",
            "a[download]",
        ],
        description="Generic download button",
    ),
}

# ── No Results / Error States ──────────────────────────────────────────────────
STATES = {
    "no_results": SelectorSet(
        primary="text='Δεν βρέθηκαν αποτελέσματα'",
        fallbacks=[
            "text='Δεν υπάρχουν αποτελέσματα'",
            ".no-results",
            "td:has-text('Δεν βρέθηκε')",
        ],
        description="'No results' indicator",
    ),
    "session_expired": SelectorSet(
        primary="text='Η συνεδρία έληξε'",
        fallbacks=[
            "text='session expired'",
            "text='Εισαγωγή'",
            ".session-timeout",
        ],
        description="Session expired indicator",
    ),
    "loading_spinner": SelectorSet(
        primary=".loading",
        fallbacks=[".spinner", "[class*='load']", ".wait"],
        description="Loading indicator to wait for",
    ),
}

ALL_SELECTORS = {
    "login": LOGIN,
    "nav": NAV,
    "search": SEARCH,
    "pdf": PDF,
    "states": STATES,
}
