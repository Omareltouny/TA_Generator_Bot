"""File uploads: outline (new course), slides/reference (files or zip), logo, template."""
from __future__ import annotations

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
from bot.services.pdf_text import extract_pdf

log = logging.getLogger("upload")
MB = 1024 * 1024
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp"}


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
    if mode == "awaiting_outline":
        got = await _download(update, ctx)
        if got:
            await _new_course(update, ctx, user, *got)
        return
    if not course_id:
        await ui.reply(update, "Use /newcourse to start a course with an outline PDF, or /courses to pick one.")
        return
    if not kind:
        await ui.reply(update, "What is this file? Choose first:", ui.kb(
            [("Lecture slides", "up:slides"), ("Past labs/tutorials", "up:reference")], [("Logo", "up:logo"), ("Template (.docx)", "up:template")]))
        await ui.reply(update, "Then send the file again.")
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
        await _save_material(update, ctx, user, course_id, kind, name, blob)


async def _new_course(update, ctx, user, name: str, blob: bytes):
    if materials.ext_of(name) != ".pdf":
        await ui.reply(update, "The outline must be a PDF. Please send it as a .pdf file.")
        return
    try:
        ext = extract_pdf(blob)
    except Exception:
        await ui.reply(update, "I couldn't read that PDF (corrupt or encrypted?). Please try another copy.")
        return
    if len(ext.empty_pages) == ext.pages:
        await ui.reply(update, "This PDF has no text layer (scanned images), and I can't OCR it. Please send a text-based PDF.")
        return
    note = f"\nNote: page(s) {ext.empty_pages} have no text layer and were skipped." if ext.empty_pages else ""
    wait = await ui.reply(update, "Reading the outline..." + note)
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
        await repo.add_material(s, course.id, "outline", name, ext.text, user.id)
        await repo.set_state(s, user.id, mode="outline_review", course_id=course.id, data={})
    await ui.reply(update, outline_parser.summarize(outline), ui.kb(
        [("Continue to plan", "oc")], [("Add slides", "up:slides"), ("Add past labs/tutorials", "up:reference")], [("Add logo", "up:logo")]))


async def _save_material(update, ctx, user, course_id, kind, name, blob):
    cap = ui.cfg(ctx).max_total_material_mb * MB
    files, skipped = ([(name, blob)], []) if materials.ext_of(name) != ".zip" else (None, None)
    if files is None:
        try:
            z = materials.read_zip(blob)
        except ValueError as e:
            await ui.reply(update, f"Zip rejected: {e}")
            return
        files, skipped = z.files, z.skipped
    added = 0
    notes = []
    async with ui.sf(ctx)() as s:
        used = await repo.material_chars(s, course_id)
        for fname, data in files:
            try:
                ex = materials.extract_text(fname, data)
            except ValueError as e:
                skipped.append(f"{fname} ({e})")
                continue
            if used + len(ex.text) > cap:
                skipped.append(f"{fname} (course material size cap reached)")
                continue
            used += len(ex.text)
            await repo.add_material(s, course_id, kind, fname, ex.text, user.id)
            added += 1
            if ex.warning:
                notes.append(f"{fname}: {ex.warning}")
        await s.commit()
    msg = f"Added {added} file(s) as {kind}."
    if notes:
        msg += "\nNotes:\n- " + "\n- ".join(notes)
    if skipped:
        msg += f"\nSkipped {len(skipped)}:\n- " + "\n- ".join(skipped[:15]) + ("\n- ..." if len(skipped) > 15 else "")
    await ui.reply(update, msg + "\nSend more, or tap Continue.", ui.kb([("Continue to plan", "oc")], [("Done adding", "up:none")]))


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
    await ui.answer(update)
    kind = update.callback_query.data.split(":")[1]
    async with ui.sf(ctx)() as s:
        st = await repo.get_state(s, user.id)
        data = dict(st.data or {})
        if kind == "none":
            data.pop("upload_kind", None)
        else:
            data["upload_kind"] = kind
        await repo.set_state(s, user.id, data=data)
    prompts = {"slides": "Send lecture slides (pdf/pptx/docx) - files or a zip.",
               "reference": "Send past labs/tutorials (pdf/docx/pptx/txt/code files, or one zip). I use them for style and difficulty, never copying verbatim.",
               "logo": "Send the logo as an image (PNG/JPG).", "template": "Send your .docx template (used for heading/body/code styles).",
               "none": "OK."}
    await ui.reply(update, prompts[kind])


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
