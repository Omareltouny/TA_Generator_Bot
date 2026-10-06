"""PDF text extraction: pymupdf first, pdfplumber for tables, tesseract OCR for pages without a text layer.

OCR is a blocking, CPU-heavy call: handlers run `extract_pdf(..., ocr=...)` through `asyncio.to_thread`.
Pages are rendered and recognised one at a time (memory on small hosts). OCR text is treated like normal text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

MIN_OCR_CHARS = 20  # an entirely scanned PDF whose OCR yields less than this is reported as unreadable


class OcrError(Exception):
    """OCR could not be used or found nothing. The message says which (shown to the user)."""


@dataclass
class OcrOptions:
    lang: str = "eng"       # tesseract format, e.g. "eng+ara"
    dpi: int = 200
    max_pages: int = 60     # pages OCR'd per file; the rest are skipped and reported


@dataclass
class Extraction:
    text: str
    pages: int
    empty_pages: list[int] = field(default_factory=list)   # 1-based pages with no text (after OCR, if it ran)
    ocr_pages: list[int] = field(default_factory=list)     # 1-based pages whose text came from OCR
    ocr_skipped: list[int] = field(default_factory=list)   # pages not OCR'd because of ocr.max_pages


def _ocr_page(page, ocr: OcrOptions) -> str:
    try:
        import pymupdf as fitz
        import pytesseract
        from PIL import Image
    except ImportError as e:  # pragma: no cover - depends on the deployment
        raise OcrError(f"OCR libraries are not installed ({e.name}); install pytesseract and Pillow") from e
    pix = page.get_pixmap(dpi=ocr.dpi, colorspace=fitz.csRGB, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    try:
        return pytesseract.image_to_string(img, lang=ocr.lang)
    except pytesseract.TesseractNotFoundError as e:
        raise OcrError("OCR engine (tesseract) is not installed on this server") from e
    except pytesseract.TesseractError as e:
        msg = str(e)
        if "language" in msg.lower() or "traineddata" in msg.lower():
            raise OcrError(f"OCR language data for '{ocr.lang}' is not installed on this server") from e
        raise OcrError(f"OCR failed: {msg[:150]}") from e
    finally:
        del img, pix


def extract_pdf(data: bytes, include_tables: bool = False, ocr: OcrOptions | None = None,
                progress: Callable[[int, int], None] | None = None) -> Extraction:
    """Text of a PDF. With `ocr`, pages with no text layer are OCR'd (at most `ocr.max_pages`).

    `progress(done, total)` is called from the calling thread after each OCR'd page (total = pages to OCR).
    Raises OcrError if OCR is needed but unusable, or if an entirely scanned PDF yields (almost) no text.
    """
    import pymupdf as fitz
    doc = fitz.open(stream=data, filetype="pdf")
    layer = [page.get_text().strip() for page in doc]
    todo = [i for i, t in enumerate(layer, 1) if not t] if ocr is not None else []
    parts, empty, ocr_pages, skipped = [], [], [], []
    attempted = 0
    total_ocr = min(len(todo), ocr.max_pages) if ocr is not None else 0
    for i, page in enumerate(doc, 1):
        t = layer[i - 1]
        if not t and ocr is not None:
            if attempted >= ocr.max_pages:
                skipped.append(i)
            else:
                t = _ocr_page(page, ocr).strip()
                attempted += 1
                if progress:
                    progress(attempted, total_ocr)
                if t:
                    ocr_pages.append(i)
        if not t:
            empty.append(i)
        parts.append(f"--- page {i} ---\n{t}")
    text = "\n".join(parts)
    if ocr is not None and len(todo) == len(layer) and len(layer) > 0:
        recovered = sum(len(p.split("\n", 1)[1]) for p in parts if "\n" in p)
        if recovered < MIN_OCR_CHARS:
            raise OcrError("OCR found no readable text in this scanned PDF (is it a clear scan, and is the OCR language right?)")
    # pdfplumber tables are OPT-IN: on the real outlines they dropped rows in merged-cell tables
    # (e.g. hid 2 of 3 assignments), while plain pymupdf text kept every row.
    try:
        if not include_tables:
            raise StopIteration
        import io
        import pdfplumber
        tables = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for i, p in enumerate(pdf.pages, 1):
                for tb in p.extract_tables():
                    rows = [" | ".join((c or "").replace("\n", " ").strip() for c in r) for r in tb if any(r)]
                    if rows:
                        tables.append(f"[table, page {i}]\n" + "\n".join(rows))
        if tables:
            text += "\n\n=== TABLES ===\n" + "\n\n".join(tables)
    except StopIteration:
        pass
    except Exception:  # table extraction is best-effort
        pass
    return Extraction(text=text, pages=len(doc), empty_pages=empty, ocr_pages=ocr_pages, ocr_skipped=skipped)


def ocr_warning(ocr_pages: list[int], skipped: list[int], max_pages: int | None = None) -> str | None:
    """User-facing warning whenever OCR was used (or pages were skipped by the OCR page cap)."""
    bits = []
    if ocr_pages:
        bits.append(f"{len(ocr_pages)} page(s) were read with OCR; check the result, equations and tables may be wrong.")
    if skipped:
        bits.append(f"{len(skipped)} page(s) were skipped because the OCR page limit ({max_pages}) was reached.")
    return " ".join(bits) or None
