"""Delivery of items, approve / regenerate / .tex export, items browser, version history (spec 6.5, 6.6.7)."""
from __future__ import annotations

import io

from sqlalchemy import select
from telegram import Update
from telegram.ext import ContextTypes

from bot.db.models import Course, PlanItem
from bot.handlers import ui
from bot.handlers.auth import require_user
from bot.handlers.courses import require_course
from bot.services import renderer, repo
from bot.services.selection import parse_selection

PAGE = 8
TYPES = ["all", "lab", "tutorial", "assignment"]
STATUSES = ["all", "planned", "draft", "approved", "failed"]


def item_buttons(item_id: int):
    return ui.kb([("Approve", f"ap:{item_id}"), ("Give feedback", f"fb:{item_id}")],
                 [("Regenerate", f"rg:{item_id}"), ("Export .tex", f"tx:{item_id}")], [("History", f"hi:{item_id}")])


def _meta(course: Course, item: PlanItem) -> renderer.DocMeta:
    return renderer.DocMeta(course.code or "", course.name or "", item.type, item.seq, item.week, item.title)


async def send_files(bot, chat_id: int, sf, item_id: int, version: int | None = None, buttons=True, note: str = ""):
    async with sf() as s:
        item = await s.get(PlanItem, item_id)
        v = await repo.get_version(s, item_id, version) if item else None
        course = await s.get(Course, item.course_id) if item else None
        if not v:
            await bot.send_message(chat_id, "No generated version for that item yet.")
            return
        meta = _meta(course, item)
        sd, kd = v.student_docx, v.key_docx
        text = (f"{item.type.title()} {item.seq} (week {item.week or '?'}) - {item.title}\n"
                f"Version {v.version} - status: {item.status}{note}\n[item #{item.id}]")
    await bot.send_document(chat_id, document=io.BytesIO(sd), filename=meta.filename("student", "docx"))
    await bot.send_document(chat_id, document=io.BytesIO(kd), filename=meta.filename("answer_key", "docx"))
    await bot.send_message(chat_id, text + "\nReply to this message with text to give feedback.",
                           reply_markup=item_buttons(item_id) if buttons else None)


def make_deliver(app):
    async def deliver(chat_id: int, item_id: int):
        await send_files(app.bot, chat_id, app.bot_data["db"], item_id)
    return deliver


async def _item(ctx, item_id: int) -> PlanItem | None:
    async with ui.sf(ctx)() as s:
        return await s.get(PlanItem, item_id)


@require_user
async def cb_approve(update, ctx, user):
    await ui.answer(update)
    iid = int(update.callback_query.data.split(":")[1])
    async with ui.sf(ctx)() as s:
        item = await s.get(PlanItem, iid)
        if not item or item.current_version == 0:
            await ui.reply(update, "Nothing to approve yet.")
            return
        item.status = "approved"
        await s.commit()
    await ui.reply(update, f"Approved {item.type} {item.seq}. You can still give feedback on it later (it will go back to draft).")


@require_user
async def cb_regenerate(update, ctx, user):
    await ui.answer(update)
    iid = int(update.callback_query.data.split(":")[1])
    course = await require_course(update, ctx, user)
    item = await _item(ctx, iid)
    if course and item:
        from bot.handlers.generate import start_job
        await start_job(update, ctx, user, course, [iid], origin="regenerate")


@require_user
async def cb_tex(update, ctx, user):
    await ui.answer(update)
    iid = int(update.callback_query.data.split(":")[1])
    async with ui.sf(ctx)() as s:
        item = await s.get(PlanItem, iid)
        v = await repo.get_version(s, iid) if item else None
        course = await s.get(Course, item.course_id) if item else None
    if not v:
        await ui.reply(update, "No generated version yet.")
        return
    meta = _meta(course, item)
    for md, key, kind in ((v.student_md, False, "student"), (v.key_md, True, "answer_key")):
        tex = await renderer.render_tex(md, meta, answer_key=key)
        await update.effective_chat.send_document(io.BytesIO(tex), filename=meta.filename(kind, "tex"))


# ---- items browser ------------------------------------------------------
async def show_items(update, ctx, user, typ="all", status="all", page=0):
    course = await require_course(update, ctx, user)
    if not course:
        return
    async with ui.sf(ctx)() as s:
        items = [i for i in await repo.load_plan(s, course.id)
                 if (typ == "all" or i.type == typ) and (status == "all" or i.status == status)]
    chunk = items[page * PAGE:(page + 1) * PAGE]
    rows = [[(ui.label(i)[:60], f"it:{i.id}")] for i in chunk]
    nav = ([("< Prev", f"il:{typ}:{status}:{page - 1}")] if page > 0 else []) + ([("Next >", f"il:{typ}:{status}:{page + 1}")] if (page + 1) * PAGE < len(items) else [])
    f1 = [((f"*{t}" if t == typ else t), f"il:{t}:{status}:0") for t in TYPES]
    f2 = [((f"*{t}" if t == status else t), f"il:{typ}:{t}:0") for t in STATUSES]
    text = f"Items ({len(items)}) - type: {typ}, status: {status}" if items else f"No items match (type: {typ}, status: {status})."
    markup = ui.kb(*rows, nav, f1, f2)
    if update.callback_query and update.callback_query.data.startswith("il:"):
        try:
            await update.callback_query.edit_message_text(text, reply_markup=markup)
            return
        except Exception:
            pass
    await ui.reply(update, text, markup)


@require_user
async def cmd_items(update, ctx, user):
    await ui.answer(update)
    await show_items(update, ctx, user)


@require_user
async def cb_items_list(update, ctx, user):
    await ui.answer(update)
    _, typ, status, page = update.callback_query.data.split(":")
    await show_items(update, ctx, user, typ, status, int(page))


@require_user
async def cb_item_detail(update, ctx, user):
    await ui.answer(update)
    iid = int(update.callback_query.data.split(":")[1])
    item = await _item(ctx, iid)
    if not item:
        await ui.reply(update, "Item not found.")
        return
    if item.current_version > 0:
        await send_files(ctx.bot, update.effective_chat.id, ui.sf(ctx), iid)
    else:
        await ui.reply(update, f"{ui.label(item)}\nNot generated yet.", ui.kb([("Generate this item", f"rg:{iid}")]))


# ---- history ------------------------------------------------------------
async def show_history(update, ctx, item: PlanItem):
    async with ui.sf(ctx)() as s:
        versions = await repo.list_versions(s, item.id)
    rows = [[(f"v{v.version} ({v.created_at:%Y-%m-%d %H:%M}, {v.llm_provider or '?'})", f"hv:{item.id}:{v.version}")] for v in versions]
    await ui.reply(update, f"History of {item.type} {item.seq} - {item.title} (current: v{item.current_version})"
                   if versions else "No versions yet.", ui.kb(*rows))


@require_user
async def cmd_history(update, ctx, user):
    course = await require_course(update, ctx, user)
    if not course:
        return
    async with ui.sf(ctx)() as s:
        items = await repo.load_plan(s, course.id)
    sel, _ = parse_selection(" ".join(ctx.args or []), items)
    if len(sel) != 1:
        await ui.reply(update, "Usage: /history <item>, e.g. /history lab 3")
        return
    await show_history(update, ctx, sel[0])


@require_user
async def cb_history(update, ctx, user):
    await ui.answer(update)
    await show_history(update, ctx, await _item(ctx, int(update.callback_query.data.split(":")[1])))


@require_user
async def cb_history_version(update, ctx, user):
    await ui.answer(update)
    _, iid, ver = update.callback_query.data.split(":")
    await ui.reply(update, f"Version {ver}:", ui.kb([("Re-send files", f"hs:{iid}:{ver}"), ("Roll back to this", f"hr:{iid}:{ver}")]))


@require_user
async def cb_history_send(update, ctx, user):
    await ui.answer(update)
    _, iid, ver = update.callback_query.data.split(":")
    await send_files(ctx.bot, update.effective_chat.id, ui.sf(ctx), int(iid), int(ver), buttons=False, note=" (historical)")


@require_user
async def cb_history_rollback(update, ctx, user):
    """Rollback = copy the old version's content as a NEW latest version (history is never rewritten)."""
    await ui.answer(update)
    _, iid, ver = update.callback_query.data.split(":")
    async with ui.sf(ctx)() as s:
        item = await s.get(PlanItem, int(iid))
        old = await repo.get_version(s, int(iid), int(ver))
        if not old:
            await ui.reply(update, "Version not found.")
            return
        v = await repo.add_version(s, item, student_md=old.student_md, key_md=old.key_md, student_docx=old.student_docx,
                                   key_docx=old.key_docx, provider=old.llm_provider, model=old.llm_model,
                                   rule_ids=old.feedback_rule_ids)
        await s.commit()
        new_no = v.version
    await ui.reply(update, f"Rolled back: v{ver} restored as new version v{new_no} (status: draft).")
    await send_files(ctx.bot, update.effective_chat.id, ui.sf(ctx), int(iid))
