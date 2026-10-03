"""PDF text extraction: pymupdf first, pdfplumber for tables. Never guess on image-only pages."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Extraction:
    text: str
    pages: int
    empty_pages: list[int] = field(default_factory=list)  # 1-based pages with no text layer


def extract_pdf(data: bytes, include_tables: bool = False) -> Extraction:
    import pymupdf as fitz
    doc = fitz.open(stream=data, filetype="pdf")
    parts, empty = [], []
    for i, page in enumerate(doc, 1):
        t = page.get_text().strip()
        if not t:
            empty.append(i)
        parts.append(f"--- page {i} ---\n{t}")
    text = "\n".join(parts)
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
    return Extraction(text=text, pages=len(doc), empty_pages=empty)
