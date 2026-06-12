"""
PDF text extraction and Greek legal document parsing.

Strategy:
  1. pdfplumber  → structured text extraction (preferred for digital PDFs)
  2. PyMuPDF     → fallback text extraction
  3. OCR         → last resort when text layer is absent/corrupt
  4. LLM         → optional, for ambiguous Greek legal phrases
"""
import re
import logging
from pathlib import Path
from typing import Optional

from config import Config
from database.db import upsert_parse_result, add_log

logger = logging.getLogger(__name__)

# ── Greek keyword patterns ─────────────────────────────────────────────────────

# Area: "ΕΜΒΑΔΟΝ: 3.842,50 τ.μ." or "Εμβαδό 3842.50"
_AREA_PATTERNS = [
    r"[Εε][Μμ][Ββ][Αα][Δδ][Οο][Νν]\s*[:\-]?\s*([\d\.,]+)\s*(?:τ\.?μ\.?|τ\.μ\.)",
    r"(?:ΕΚΤΑΣΗ|Εκταση)\s*[:\-]?\s*([\d\.,]+)\s*(?:τ\.?μ\.?|τ\.μ\.)",
    r"([\d\.,]+)\s*(?:τ\.?μ\.?|τ\.μ\.)\s*",
]

# Building presence
_BUILDING_YES = [
    r"[Κκ][Ττ][Ίίι][Σσ][Μμ][Αα]",
    r"[Ββ][Ιι][Οο][Μμ][Ηη][Χχ][Αα][Νν][Ίίι][Αα]",
    r"[Οο][Ιι][Κκ][Ίίι][Αα]",
    r"ΚΤΙΡΙΟ",
    r"ΟΙΚΟΔΟΜΗ",
]
_BUILDING_NO = [
    r"ΑΓΡΟΤΕΜΑΧΙΟ",
    r"ΑΓΡΟΤΙΚΟ\s+ΤΕΜΑΧΙΟ",
    r"ΧΩΡΙΣ\s+ΚΤΙΣΜΑ",
    r"ΑΟΙΚΟΔΟΜΗΤΟ",
]

# Burdens / encumbrances
_BURDENS_YES = [
    r"ΥΠΟΘΗΚΗ",
    r"ΠΡΟΣΗΜΕΙΩΣΗ",
    r"ΚΑΤΑΣΧΕΣΗ",
    r"ΔΙΕΚΔΙΚΗΣΗ",
    r"ΒΑΡΗ",
    r"ΒΑΡΟΣ",
    r"ΔΟΥΛΕΙΑ",
    r"ΕΜΠΡΑΓΜΑΤΟ\s+ΒΑΡΟΣ",
]
_BURDENS_NONE = [
    r"ΧΩΡΙΣ\s+ΒΑΡΗ",
    r"ΕΛΕΥΘΕΡΟ\s+ΒΑΡΩΝ",
    r"ΟΥΔΕΝ\s+ΒΑΡΟΣ",
]

# KAEK pattern in document
_KAEK_PATTERN = r"\b(\d{2}\d{3}\d{4}/\d{2}/\d+/\d+)\b"

# Property type keywords
_PROPERTY_TYPES = {
    "αγροτεμάχιο": r"ΑΓΡΟΤΕΜΑΧΙΟ",
    "αστικό": r"ΑΣΤΙΚ",
    "δασικό": r"ΔΑΣΙΚ",
    "αγροτικό": r"ΑΓΡΟΤΙΚ",
    "οικόπεδο": r"ΟΙΚΟΠΕΔ",
    "διαμέρισμα": r"ΔΙΑΜΕΡΙΣΜ",
    "μονοκατοικία": r"ΜΟΝΟΚΑΤΟΙΚ",
}


def _normalize_greek_number(s: str) -> Optional[float]:
    """Convert Greek-locale number string to float. E.g. '3.842,50' → 3842.5"""
    s = s.strip().replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def extract_text_pdfplumber(pdf_path: Path) -> str:
    """Extract all text from PDF using pdfplumber."""
    try:
        import pdfplumber
        with pdfplumber.open(str(pdf_path)) as pdf:
            pages = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
        return "\n".join(pages)
    except Exception as exc:
        logger.debug("pdfplumber failed for %s: %s", pdf_path, exc)
        return ""


def extract_text_pymupdf(pdf_path: Path) -> str:
    """Fallback text extraction using PyMuPDF (fitz)."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(str(pdf_path))
        pages = []
        for page in doc:
            pages.append(page.get_text())
        doc.close()
        return "\n".join(pages)
    except Exception as exc:
        logger.debug("PyMuPDF failed for %s: %s", pdf_path, exc)
        return ""


def extract_area(text: str) -> Optional[float]:
    text_upper = text.upper()
    for pattern in _AREA_PATTERNS:
        for m in re.finditer(pattern, text_upper):
            val = _normalize_greek_number(m.group(1))
            if val and 10 < val < 10_000_000:  # sanity range in sq.m.
                return val
    return None


def detect_building(text: str) -> str:
    text_upper = text.upper()
    for p in _BUILDING_NO:
        if re.search(p, text_upper):
            return "no"
    for p in _BUILDING_YES:
        if re.search(p, text_upper):
            return "yes"
    return "unknown"


def detect_burdens(text: str) -> tuple[str, str]:
    """Returns (has_burdens, burdens_detail)."""
    text_upper = text.upper()
    for p in _BURDENS_NONE:
        if re.search(p, text_upper):
            return "no", ""
    details = []
    for p in _BURDENS_YES:
        m = re.search(p, text_upper)
        if m:
            # Grab a window of text around the match for context
            start = max(0, m.start() - 20)
            end = min(len(text_upper), m.end() + 80)
            snippet = text[start:end].strip()
            details.append(snippet)
    if details:
        return "yes", " | ".join(details[:3])
    return "unknown", ""


def detect_property_type(text: str) -> Optional[str]:
    text_upper = text.upper()
    for label, pattern in _PROPERTY_TYPES.items():
        if re.search(pattern, text_upper):
            return label
    return None


def extract_kaek_from_text(text: str) -> Optional[str]:
    m = re.search(_KAEK_PATTERN, text)
    return m.group(1) if m else None


def _compute_confidence(data: dict) -> float:
    """Simple heuristic confidence based on how many fields were extracted."""
    score = 0.0
    if data.get("area_sqm"):
        score += 0.4
    if data.get("has_building") != "unknown":
        score += 0.2
    if data.get("has_burdens") != "unknown":
        score += 0.2
    if data.get("property_type"):
        score += 0.1
    if len(data.get("raw_text", "")) > 200:
        score += 0.1
    return round(score, 2)


def parse_text(raw_text: str, parse_method: str = "text") -> dict:
    """Parse Greek legal text and return structured fields."""
    area_sqm = extract_area(raw_text)
    has_building = detect_building(raw_text)
    has_burdens, burdens_detail = detect_burdens(raw_text)
    property_type = detect_property_type(raw_text)

    data = {
        "raw_text": raw_text[:5000],  # Store first 5k chars
        "area_sqm": area_sqm,
        "area_stremma": round(area_sqm / 1000, 4) if area_sqm else None,
        "has_building": has_building,
        "has_burdens": has_burdens,
        "property_type": property_type,
        "burdens_detail": burdens_detail or None,
        "ownership_info": None,  # TODO: extract ownership from text
        "notes": None,
        "parse_method": parse_method,
    }
    data["confidence"] = _compute_confidence(data)
    return data


def parse_pdf_file(kaek: str, pdf_type: str, pdf_path: Path) -> dict:
    """
    Full parsing pipeline for a single PDF file.
    Tries pdfplumber → PyMuPDF → OCR.
    Stores result in DB and returns the parsed dict.
    """
    if not pdf_path.exists():
        add_log(kaek, f"parse_{pdf_type}", "failure", f"PDF not found: {pdf_path}")
        return {}

    # 1. pdfplumber
    raw_text = extract_text_pdfplumber(pdf_path)
    method = "text_pdfplumber"

    # 2. PyMuPDF fallback
    if len(raw_text.strip()) < 50:
        raw_text = extract_text_pymupdf(pdf_path)
        method = "text_pymupdf"

    # 3. OCR fallback
    if len(raw_text.strip()) < 50:
        try:
            from parsing.ocr_helper import ocr_pdf
            raw_text = ocr_pdf(pdf_path)
            method = "ocr"
        except Exception as exc:
            logger.warning("OCR failed for %s: %s", pdf_path, exc)

    # 4. Optional LLM classification
    if Config.LLM_ENABLED and raw_text:
        try:
            data = _llm_classify(kaek, pdf_type, raw_text)
            data["parse_method"] = "llm"
        except Exception as exc:
            logger.warning("LLM classification failed: %s", exc)
            data = parse_text(raw_text, method)
    else:
        data = parse_text(raw_text, method)

    upsert_parse_result(kaek, pdf_type, data)
    add_log(
        kaek,
        f"parse_{pdf_type}",
        "success",
        f"method={data['parse_method']} area={data.get('area_sqm')} conf={data.get('confidence')}",
    )
    return data


def _llm_classify(kaek: str, pdf_type: str, raw_text: str) -> dict:
    """
    Use Claude to classify ambiguous Greek legal text.
    Only called when ANTHROPIC_API_KEY is set.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)

    prompt = f"""You are analyzing a Greek Ktimatologio (land registry) document.
Extract the following fields from the text and respond in JSON only:
- area_sqm: numeric area in square meters (null if not found)
- has_building: "yes", "no", or "unknown"
- has_burdens: "yes", "no", or "unknown" (υποθήκες/βάρη/κατασχέσεις)
- property_type: brief description in Greek (null if not found)
- burdens_detail: brief description of any burdens found (null if none)
- notes: any important notes or restrictions (null if none)

Document type: {pdf_type}
KAEK: {kaek}

Document text:
{raw_text[:3000]}

Respond with a JSON object only. No explanation."""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    import json
    text_response = message.content[0].text.strip()
    # Extract JSON from response
    json_match = re.search(r"\{.*\}", text_response, re.DOTALL)
    if json_match:
        parsed = json.loads(json_match.group())
        area_sqm = parsed.get("area_sqm")
        return {
            "raw_text": raw_text[:5000],
            "area_sqm": float(area_sqm) if area_sqm else None,
            "area_stremma": round(float(area_sqm) / 1000, 4) if area_sqm else None,
            "has_building": parsed.get("has_building", "unknown"),
            "has_burdens": parsed.get("has_burdens", "unknown"),
            "property_type": parsed.get("property_type"),
            "burdens_detail": parsed.get("burdens_detail"),
            "ownership_info": None,
            "notes": parsed.get("notes"),
            "confidence": 0.85,  # LLM gets high confidence by default
        }
    raise ValueError("LLM response did not contain valid JSON")
