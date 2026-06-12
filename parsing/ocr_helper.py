"""
Tesseract OCR fallback for scanned/image-based PDFs.
Requires: tesseract, tesseract-lang-ell (Greek), pdf2image, Pillow.
"""
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def ocr_pdf(pdf_path: Path, language: str = "ell+eng") -> str:
    """
    Convert each PDF page to an image and run Tesseract OCR.
    Returns concatenated text from all pages.
    """
    try:
        from pdf2image import convert_from_path
        import pytesseract
    except ImportError as exc:
        raise ImportError(
            "OCR requires: pip install pdf2image pytesseract Pillow\n"
            "And tesseract with Greek language pack installed."
        ) from exc

    try:
        images = convert_from_path(str(pdf_path), dpi=300)
    except Exception as exc:
        logger.error("pdf2image failed for %s: %s", pdf_path, exc)
        raise

    texts = []
    for i, img in enumerate(images):
        # Preprocess: convert to grayscale for better OCR
        try:
            from PIL import ImageEnhance, ImageFilter
            img = img.convert("L")  # Grayscale
            img = img.filter(ImageFilter.SHARPEN)
            enhancer = ImageEnhance.Contrast(img)
            img = enhancer.enhance(2.0)
        except Exception:
            pass  # Use original image if preprocessing fails

        try:
            page_text = pytesseract.image_to_string(
                img,
                lang=language,
                config="--psm 3 --oem 1",  # Auto page segmentation, LSTM engine
            )
            texts.append(page_text)
            logger.debug("OCR page %d: %d chars", i + 1, len(page_text))
        except Exception as exc:
            logger.warning("OCR failed on page %d of %s: %s", i + 1, pdf_path, exc)

    return "\n".join(texts)


def ocr_image(image_path: Path, language: str = "ell+eng") -> str:
    """OCR a single image file."""
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(str(image_path))
        return pytesseract.image_to_string(img, lang=language)
    except ImportError as exc:
        raise ImportError("OCR requires pytesseract and Pillow") from exc
    except Exception as exc:
        logger.error("Image OCR failed for %s: %s", image_path, exc)
        raise


def check_tesseract_available() -> bool:
    """Check if tesseract is installed and Greek language pack is present."""
    try:
        import pytesseract
        langs = pytesseract.get_languages()
        has_greek = "ell" in langs
        if not has_greek:
            logger.warning(
                "Tesseract Greek language pack (ell) not found. "
                "Install with: brew install tesseract-lang (macOS) or "
                "sudo apt-get install tesseract-ocr-ell (Ubuntu)"
            )
        return has_greek
    except Exception:
        return False
