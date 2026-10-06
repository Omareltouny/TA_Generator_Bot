"""File uploads: outline (new course), slides/reference (files or zip), logo, template."""
from __future__ import annotations

import asyncio
import io
import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot.db.models import Course, Material, Template
from bot.handlers import ui
from bot.handlers.auth import require_user
from bot.handlers.courses import course_title, require_course
from bot.services import materials, outline_parser, renderer, repo
from bot.services.llm_router import AllProvidersBusy
from bot.services.pdf_text import OcrError, extract_pdf, ocr_warning

log = logging.getLogger("upload")
MB = 1024 * 1024
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp"}
TYPES = ("lab", "tutorial", "assignment")
PLURAL = {"lab": "labs", "tutorial": "tutorials", "assignment": "assignments"}
TYPE_PICKER = ui.kb([("Labs", "up:ref:lab"), ("Tutorials", "up:ref:tutorial"), ("Assignments", "up:ref:assignment")])


class OcrProgress:
    """Progress reporter called from the OCR worker thread: edits ONE message ("Reading scanned pages 4/12...")
    through the throttled LiveMessage, and only when more than 3 pages need OCR."""

    def __init__(self, loop, live):
        self.loop, self.live, self._futs = loop, live, []

    def __call__(self, done: int, total: int) -> None:
        if total > 3:
            self._futs.append(asyncio.run_coroutine_threadsafe(
                self.live.update(f"Reading scanned pages {done}/{total}..."), self.loop))

    async def wait(self) -> None:
        for f in self._futs:
            await asyncio.wrap_future(f)


async def _download(update: Update, ctx) -> tuple[str, bytes] | None:
    msg = update.effective_message
    if msg.document:
        f, name, size = msg.document, msg.document.file_name or "file", msg.document.file_size or 0
    elif msg.photo:
        f, name, size = msg.photo[-1], "logo.jpg", msg.photo[-1].file_size or 0
    else:
        return None
    limit = ui.cfg(ctx).max_file_mb * MB
    if size > limit:
        await ui.reply(update, f"{name} is {size / MB:.1f} MB; the limit is {ui.cfg(ctx).max_file_mb} MB. Please split or zip it into smaller parts.")
        return None
    data = bytes(await (await f.get_file()).download_as_bytearray())
    return name, data


@require_user
async def on_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user):
    async with ui.sf(ctx)() as s:
        st = await repo.get_state(s, user.id)
        await s.commit()
        mode, data = st.mode, dict(st.data or {})
        course_id = st.course_id
    kind = data.get("upload_kind")
    item_type = data.get("upload_item_type")
    if mode == "awaiting_outline":
        got = await _download(update, ctx)
        if got:
            await _new_course(update, ctx, user, *got)
        return
    if not course_id:
        await ui.reply(update, "Use /newcourse to start a course with an outline PDF, or /courses to pick one.")
        return
    if not kind:
        await ui.reply(update, "What is this file? Choose first, then send the file again:", ui.kb(
            [("Lecture slides", "up:slides"), ("Past worksheets", "up:reference")], [("Logo", "up:logo"), ("Template (.docx)", "up:template")]))
        return
    if kind == "reference" and item_type not in TYPES:
        await ui.reply(update, "Which type of worksheets are these? Choose first, then send the file again:", TYPE_PICKER)
        return
    got = await _download(update, ctx)
    if not got:
        return
    name, blob = got
    if kind == "logo":
        await _save_logo(update, ctx, user, course_id, name, blob)
    elif kind == "template":
        await _save_template(update, ctx, user, course_id, name, blob)
    else:
        await _save_material(update, ctx, user, course_id, kind, name, blob, item_type)


async def _new_course(update, ctx, user, name: str, blob: bytes):
    if materials.ext_of(name) != ".pdf":
        await ui.reply(update, "The outline must be a PDF. Please send it as a .pdf file.")
        return
    ocr = ui.ocr_options(ctx)
    wait = await ui.reply(update, "Reading the outline...")
    prog = OcrProgress(asyncio.get_running_loop(), ui.live(ctx.bot_data, ctx.bot, update.effective_chat.id, wait.message_id))
    try:
        ext = await asyncio.to_thread(extract_pdf, blob, False, ocr, prog)
    except OcrError as e:
        await ui.edit_or_send(ctx.bot, update.effective_chat.id, wait.message_id, f"I couldn't read this scanned PDF: {e}.")
        return
    except Exception:
        await ui.edit_or_send(ctx.bot, update.effective_chat.id, wait.message_id,
                              "I couldn't read that PDF (corrupt or encrypted?). Please try another copy.")
        return
    await prog.wait()
    if len(ext.empty_pages) == ext.pages:
        await ui.edit_or_send(ctx.bot, update.effective_chat.id, wait.message_id,
                              "This PDF has no text layer (scanned images) and OCR is disabled on this server (OCR_ENABLED=0). "
                              "Please send a text-based PDF.")
        return
    skipped_ocr = set(ext.ocr_skipped)
    blank = [p for p in ext.empty_pages if p not in skipped_ocr]
    warn = ocr_warning(ext.ocr_pages, ext.ocr_skipped, ocr.max_pages if ocr else None)
    note = ("\n" + warn if warn else "") + (f"\nNote: page(s) {blank} have no text and were skipped." if blank else "")
    await ui.edit_or_send(ctx.bot, update.effective_chat.id, wait.message_id, "Reading the outline..." + (" (scanned pages read with OCR)" if ext.ocr_pages else ""))
    try:
        outline, _ = await outline_parser.parse_outline(ui.llm(ctx), ext.text)
    except AllProvidersBusy as e:
        await ui.reply(update, f"All AI providers are rate-limited. Please resend the outline in ~{int(e.retry_after)} s.")
        return
    except ValueError as e:
        await ui.reply(update, f"I couldn't parse that outline into a valid structure ({str(e)[:150]}). Please try again.")
        return
    async with ui.sf(ctx)() as s:
        course = Course(created_by_user_id=user.id)
        s.add(course)
        await s.flush()
        await repo.apply_outline(course, outline.model_dump())
        await repo.add_material(s, course.id, "outline", name, ext.text, user.id, ocr_pages=len(ext.ocr_pages) or None)
        await repo.set_state(s, user.id, mode="outline_review", course_id=course.id, data={})
    await ui.reply(update, outline_parser.summarize(outline) + note, ui.kb(
        [("Continue to plan", "oc")], [("Add slides", "up:slides"), ("Add past worksheets", "up:reference")], [("Add logo", "up:logo")]))


def upload_text(agg: dict) -> str:
    """The ONE confirmation message of an upload burst, rebuilt from the running totals."""
    n, kind, t = agg["added"], agg["kind"], agg.get("type")
    if kind == "reference":
        head = f"Added {n} worksheet{'s' if n != 1 else ''} for {PLURAL.get(t, t)}"
    else:
        head = f"Added {n} file(s) as {kind}"
    if agg.get("ocr_files"):
        head += f" ({agg['ocr_files']} scanned, OCR used)"
    msg = head + "."
    if agg.get("notes"):
        msg += "\nNotes:\n- " + "\n- ".join(agg["notes"][:10]) + ("\n- ..." if len(agg["notes"]) > 10 else "")
    sk = agg.get("skipped") or []
    if sk:
        msg += f"\nSkipped {len(sk)}:\n- " + "\n- ".join(sk[:15]) + ("\n- ..." if len(sk) > 15 else "")
    return msg + "\nSend more files, or choose below."


def upload_markup(agg: dict):
    if agg["kind"] == "reference":
        return ui.kb([("Build format spec", f"ws:build:{agg['type']}"), ("Add more", "up:more"), ("Done", "up:none")])
    return ui.kb([("Continue to plan", "oc")], [("Done adding", "up:none")])


async def _save_material(update, ctx, user, course_id, kind, name, blob, item_type=None):
    cap = ui.cfg(ctx).max_total_material_mb * MB
    chat_id = update.effective_chat.id
    files, skipped = ([(name, blob)], []) if materials.ext_of(name) != ".zip" else (None, None)
    if files is None:
        try:
            z = materials.read_zip(blob)
        except ValueError as e:
            await ui.reply(update, f"Zip rejected: {e}")
            return
        files, skipped = z.files, z.skipped
    async with ui.sf(ctx)() as s:
        st = await repo.get_state(s, user.id)
        agg = dict((st.data or {}).get("up") or {})
    if not agg or agg.get("kind") != kind or agg.get("type") != item_type:   # a new burst: new message
        agg = {"kind": kind, "type": item_type, "added": 0, "ocr_files": 0, "notes": [], "skipped": [], "msg": None}
    if not agg["msg"]:
        agg["msg"] = (await ui.reply(update, "Reading files...")).message_id
    ocr, limit = ui.ocr_options(ctx), ui.cfg(ctx).examples_per_type
    prog = OcrProgress(asyncio.get_running_loop(), ui.live(ctx.bot_data, ctx.bot, chat_id, agg["msg"]))
    agg["skipped"] = [*agg["skipped"], *skipped]
    async with ui.sf(ctx)() as s:
        used = await repo.material_chars(s, course_id)
        n_examples = len([m for m in await repo.reference_materials(s, course_id, item_type) if m.is_example]) if kind == "reference" else 0
        for fname, data in files:
            try:
                ex = await asyncio.to_thread(materials.extract_text, fname, data, ocr, prog)
            except ValueError as e:
                agg["skipped"].append(f"{fname} ({e})")
                continue
            if used + len(ex.text) > cap:
                agg["skipped"].append(f"{fname} (course material size cap reached)")
                continue
            used += len(ex.text)
            is_example = kind == "reference" and n_examples < limit
            n_examples += is_example
            await repo.add_material(s, course_id, kind, fname, ex.text, user.id, ocr_pages=len(ex.ocr_pages) or None,
                                    item_type=item_type if kind == "reference" else None, is_example=is_example)
            agg["added"] += 1
            agg["ocr_files"] += 1 if ex.ocr_pages else 0
            if ex.warning:
                agg["notes"].append(f"{fname}: {ex.warning}")
        await s.commit()
    await prog.wait()
    agg["msg"] = await ui.edit_or_send(ctx.bot, chat_id, agg["msg"], upload_text(agg), upload_markup(agg))
    async with ui.sf(ctx)() as s:
        st = await repo.get_state(s, user.id)
        await repo.set_state(s, user.id, data={**(st.data or {}), "up": agg})


async def _save_logo(update, ctx, user, course_id, name, blob):
    try:
        from docx.image.image import Image
        Image.from_blob(blob)
    except Exception:
        await ui.reply(update, "I couldn't read that as an image. Send a PNG or JPG.")
        return
    async with ui.sf(ctx)() as s:
        (await s.get(Course, course_id)).logo_blob = blob
        await repo.add_material(s, course_id, "logo", name, None, user.id)
        await s.commit()
    await ui.reply(update, "Logo saved. It will appear in the header of every document generated from now on "
                           "(already generated items keep their old header until regenerated).")


async def _save_template(update, ctx, user, course_id, name, blob):
    try:
        missing = renderer.validate_template(blob)
    except ValueError as e:
        await ui.reply(update, str(e))
        return
    async with ui.sf(ctx)() as s:
        t = Template(name=name[:200], data=blob, uploaded_by=user.id)
        s.add(t)
        await s.flush()
        (await s.get(Course, course_id)).template_id = t.id
        await s.commit()
    warn = f"\nWarning: the template lacks these styles, defaults will be used for them: {', '.join(missing)}." if missing else ""
    await ui.reply(update, f"Template '{name}' saved and selected for this course.{warn}")


@require_user
async def cb_upload_kind(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user):
    kind = update.callback_query.data.split(":")[1]
    if kind == "more":
        await ui.answer(update, "Send the next file(s).")
        return
    await ui.answer(update)
    async with ui.sf(ctx)() as s:
        st = await repo.get_state(s, user.id)
        data = dict(st.data or {})
        data.pop("up", None)  # a new burst gets its own confirmation message
        if kind == "none":
            data.pop("upload_kind", None)
            data.pop("upload_item_type", None)
        else:
            data["upload_kind"] = kind
            data.pop("upload_item_type", None)
        await repo.set_state(s, user.id, data=data)
    if kind == "reference":
        await ui.reply(update, "Which type of past worksheets are you adding? (PDFs, or one zip)", TYPE_PICKER)
        return
    prompts = {"slides": "Send lecture slides (pdf/pptx/docx) - files or a zip.",
               "logo": "Send the logo as an image (PNG/JPG).", "template": "Send your .docx template (used for heading/body/code styles).",
               "none": "OK."}
    await ui.reply(update, prompts[kind])


@require_user
async def cb_upload_ref(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user):
    """up:ref:<type> - the user chose which handout type the past worksheets belong to."""
    await ui.answer(update)
    t = update.callback_query.data.split(":")[2]
    if t not in TYPES:
        return
    async with ui.sf(ctx)() as s:
        st = await repo.get_state(s, user.id)
        data = {**(st.data or {}), "upload_kind": "reference", "upload_item_type": t}
        data.pop("up", None)
        await repo.set_state(s, user.id, data=data)
    await ui.reply(update, f"Send past {PLURAL[t]} as PDFs (or one zip). The first {ui.cfg(ctx).examples_per_type} become style "
                           "examples attached to prompts; I can also extract their format into editable rules. "
                           "I never copy their content.")


@require_user
async def cmd_logo(update, ctx, user):
    if not await require_course(update, ctx, user):
        return
    async with ui.sf(ctx)() as s:
        st = await repo.get_state(s, user.id)
        await repo.set_state(s, user.id, data={**(st.data or {}), "upload_kind": "logo"})
    await ui.reply(update, "Send the logo image (PNG/JPG). It replaces any existing logo for this course.")


@require_user
async def cmd_template(update, ctx, user):
    course = await require_course(update, ctx, user)
    if not course:
        return
    await ui.answer(update)
    async with ui.sf(ctx)() as s:
        tpls = await repo.list_templates(s)
    rows = [[("Default template", "tp:0")]] + [[(f"{t.name[:40]}", f"tp:{t.id}")] for t in tpls[:15]] + [[("Upload a new .docx template", "up:template")]]
    await ui.reply(update, f"Output template for {course_title(course)} (current: {'custom #' + str(course.template_id) if course.template_id else 'default'}):", ui.kb(*rows))


@require_user
async def cb_template(update, ctx, user):
    await ui.answer(update)
    course = await require_course(update, ctx, user)
    if not course:
        return
    tid = int(update.callback_query.data.split(":")[1])
    async with ui.sf(ctx)() as s:
        (await s.get(Course, course.id)).template_id = tid or None
        await s.commit()
    await ui.reply(update, "Template set to " + ("default." if not tid else f"#{tid}.") + " It applies to documents generated from now on.")
