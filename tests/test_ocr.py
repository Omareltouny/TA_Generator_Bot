"""OCR for scanned PDFs (rewrite spec 9, test 12). Skipped when the tesseract binary is missing."""
import io
import shutil

import pytest

from bot.services import materials
from bot.services.pdf_text import OcrError, OcrOptions, extract_pdf

needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract binary not installed")


def scanned_pdf(pages: list[str]) -> bytes:
    """An image-only PDF: text is rasterised with Pillow, so the PDF has no text layer."""
    PIL = pytest.importorskip("PIL.Image")
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.load_default(size=56)
    imgs = []
    for text in pages:
        img = Image.new("RGB", (1240, 800), "white")
        d = ImageDraw.Draw(img)
        for n, line in enumerate(text.split("\n")):
            d.text((60, 60 + n * 100), line, fill="black", font=font)
        imgs.append(img)
    buf = io.BytesIO()
    imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:], resolution=150)
    return buf.getvalue()


@needs_tesseract
def test_ocr_pdf():
    pdf = scanned_pdf(["Course Outline\nWeek one integration", "Assignment marks total"])
    plain = extract_pdf(pdf)
    assert plain.empty_pages == [1, 2] and not plain.ocr_pages          # really no text layer
    ex = extract_pdf(pdf, ocr=OcrOptions())
    assert ex.ocr_pages == [1, 2] and not ex.empty_pages
    low = ex.text.lower()
    assert "outline" in low and "integration" in low and "assignment" in low
    seen = []
    extract_pdf(pdf, ocr=OcrOptions(), progress=lambda d, t: seen.append((d, t)))
    assert seen == [(1, 2), (2, 2)]


@needs_tesseract
def test_ocr_page_cap_and_warnings():
    pdf = scanned_pdf(["First page text here", "Second page text here"])
    ex = extract_pdf(pdf, ocr=OcrOptions(max_pages=1))
    assert ex.ocr_pages == [1] and ex.ocr_skipped == [2]
    out = materials.extract_text("s.pdf", pdf, OcrOptions(max_pages=1))
    assert "1 page(s) were read with OCR; check the result, equations and tables may be wrong." in out.warning
    assert "OCR page limit (1)" in out.warning and out.ocr_pages == [1]


@needs_tesseract
def test_materials_extract_text_uses_ocr_and_reports_disabled():
    pdf = scanned_pdf(["Binary search tree notes"])
    assert "binary" in materials.extract_text("s.pdf", pdf, OcrOptions()).text.lower()
    with pytest.raises(ValueError, match="OCR is disabled"):
        materials.extract_text("s.pdf", pdf, None)


@needs_tesseract
def test_ocr_blank_scan_reports_nothing_found():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), "white").save(buf, "PDF")
    with pytest.raises(OcrError, match="no readable text"):
        extract_pdf(buf.getvalue(), ocr=OcrOptions())


def test_ocr_engine_missing_is_reported(monkeypatch):
    pytest.importorskip("pytesseract")
    import pytesseract
    pdf_bytes = None
    try:
        pdf_bytes = scanned_pdf(["x"])
    except Exception:
        pytest.skip("cannot build a scanned pdf here")

    def boom(*a, **k):
        raise pytesseract.TesseractNotFoundError()
    monkeypatch.setattr(pytesseract, "image_to_string", boom)
    with pytest.raises(ValueError, match="tesseract"):
        materials.extract_text("s.pdf", pdf_bytes, OcrOptions())


@pytest.fixture
async def ocr_world(tmp_path):
    from tests.test_e2e_telegram import Base, Config, FakeRequest, build_app, make_llm, post_init
    from sqlalchemy.ext.asyncio import create_async_engine

    async def make(env_extra):
        cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "123:ABC", "INVITE_TOKEN": "s3cret", "OWNER_TELEGRAM_ID": "900",
                               "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path}/o{len(env_extra)}.db", "GROQ_API_KEY": "x", **env_extra})
        req = FakeRequest()
        app = build_app(cfg, request=req)
        eng = create_async_engine(cfg.database_url)
        async with eng.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        app.bot_data["llm"] = make_llm()
        await app.initialize()
        await post_init(app)
        app.bot_data["worker_task"].cancel()
        return app, req, eng
    made = []

    async def factory(env_extra=None):
        w = await make(env_extra or {})
        made.append(w)
        return w[0], w[1]
    yield factory
    for app, req, eng in made:
        await app.shutdown()
        await eng.dispose()


@needs_tesseract
async def test_scanned_outline_e2e(ocr_world):
    from tests.tg_harness import Client
    app, req = await ocr_world()
    ta = Client(app, req, 111)
    await ta.say("/start"); await ta.say("s3cret"); await ta.say("/newcourse")
    await ta.send_file("scan.pdf", scanned_pdf(["Calculus outline page one", "Grading table page two"]))
    texts = "\n".join(req.texts(111))
    assert "MATH 1920" in texts                                     # outline parsed (LLM stubbed) from OCR text
    assert "2 page(s) were read with OCR; check the result, equations and tables may be wrong." in texts
    async with app.bot_data["db"]() as s:
        from sqlalchemy import select
        from bot.db.models import Material
        m = (await s.execute(select(Material).where(Material.kind == "outline"))).scalar_one()
        assert m.ocr_pages == 2 and "calculus" in m.extracted_text.lower()


async def test_scanned_outline_with_ocr_disabled_says_so(ocr_world):
    from tests.tg_harness import Client
    pytest.importorskip("PIL")
    app, req = await ocr_world({"OCR_ENABLED": "0"})
    ta = Client(app, req, 111)
    await ta.say("/start"); await ta.say("s3cret"); await ta.say("/newcourse")
    await ta.send_file("scan.pdf", scanned_pdf(["Calculus outline"]))
    assert any("OCR is disabled" in t for t in req.texts(111))


@needs_tesseract
async def test_ocr_progress_message_for_long_scans(ocr_world):
    from tests.tg_harness import Client
    app, req = await ocr_world({"STATUS_EDIT_MIN_INTERVAL_S": "0"})
    ta = Client(app, req, 111)
    await ta.say("/start"); await ta.say("s3cret"); await ta.say("/newcourse")
    await ta.send_file("scan.pdf", scanned_pdf([f"Outline page number {i}" for i in range(1, 6)]))
    assert any("Reading scanned pages 5/5..." in t for t in req.texts(111))
    # it is the SAME message that was edited (one status message, not one per page)
    waits = [m for m in req.sent if m[3] == "Reading the outline..."]
    assert len(waits) == 1
