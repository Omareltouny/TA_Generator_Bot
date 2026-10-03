import pathlib

import pytest

from bot.services.pdf_text import extract_pdf

FIX = pathlib.Path(__file__).parent / "fixtures"
ALL = sorted(FIX.glob("*.pdf"))


@pytest.mark.parametrize("pdf", ALL, ids=lambda p: p.stem)
def test_all_fixtures_have_text_layer(pdf):
    e = extract_pdf(pdf.read_bytes())
    assert not e.empty_pages and len(e.text) > 5000


def test_math1920_keeps_all_assignments_and_worksheets():
    t = extract_pdf((FIX / "math1920.pdf").read_bytes()).text
    assert all(f"Assignment {i}" in t for i in (1, 2, 3))
    assert all(f"Worksheet {i}" in t for i in range(1, 13))
