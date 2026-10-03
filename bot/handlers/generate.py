"""Generation menu, item selection, job creation, cancel (spec 6.4)."""
from __future__ import annotations

from sqlalchemy import select, update as sa_update

from telegram import Update
from telegram.ext import ContextTypes

from bot.db.models import Job, PlanItem
from bot.handlers import ui
from bot.handlers.auth import require_user
from bot.handlers.courses import require_course
from bot.services import repo
from bot.services.selection import parse_selection

PAGE = 8


async def show_menu(update: Update, ctx, user):
    course = await require_course(update, ctx, user)
    if not course:
        return
    if not course.plan_confirmed:
        await ui.reply(update, "Confirm the plan first (/plan).")
        return
    await ui.reply(update, "What should I generate? (Everything / All X only generate items that are still planned or failed; "
                           "'Pick items' can also regenerate existing ones.)",
                   ui.kb([("Everything", "g:all")], [("All labs", "g:lab"), ("All tutorials", "g:tutorial"), ("All assignments", "g:assignment")],
                         [("Pick items", "g:pick")]))


@require_user
async def cmd_generate(update, ctx, user):
    await ui.answer(update)
    await show_menu(update, ctx, user)


async def start_job(update, ctx, user, course, item_ids: list[int], origin: str = "generate"):
    if not item_ids:
        await ui.reply(update, "Nothing to generate (everything in that selection is already generated). Use Pick items to regenerate.")
        return
    chat_id = update.effective_chat.id
    msg = await ui.reply(update, f"Queued {len(item_ids)} item(s). Starting shortly...")
    async with ui.sf(ctx)() as s:
        await repo.create_job(s, course.id, "generate", {"item_ids": item_ids, "chat_id": chat_id, "progress_msg_id": msg.message_id, "origin": origin}, user.id)
        await repo.set_state(s, user.id, mode=None)


@require_user
async def cb_generate(update: Update, ctx, user):
    await ui.answer(update)
    course = await require_course(update, ctx, user)
    if not course or not course.plan_confirmed:
        await ui.reply(update, "Confirm the plan first (/plan).")
        return
    scope = update.callback_query.data.split(":")[1]
    async with ui.sf(ctx)() as s:
        items = await repo.load_plan(s, course.id)
    if scope == "pick":
        async with ui.sf(ctx)() as s:
            await repo.set_state(s, user.id, mode="pick", data={"sel": [], "page": 0})
        await show_pick(update, ctx, items, [], 0, edit=False)
        return
    todo = [i.id for i in items if (scope == "all" or i.type == scope) and i.status in ("planned", "failed")]
    await start_job(update, ctx, user, course, todo)


async def show_pick(update, ctx, items, sel, page, edit=True):
    chunk = items[page * PAGE:(page + 1) * PAGE]
    rows = [[(("[x] " if i.id in sel else "[ ] ") + ui.label(i)[:55], f"gt:{i.id}")] for i in chunk]
    nav = ([("< Prev", "gp:" + str(page - 1))] if page > 0 else []) + ([("Next >", "gp:" + str(page + 1))] if (page + 1) * PAGE < len(items) else [])
    rows += [nav, [(f"Generate {len(sel)} selected", "gg"), ("Cancel", "gx")]]
    text = ("Select items (tap to toggle), or type e.g. \"labs 2-5 and assignment 1\".\n"
            f"Page {page + 1}/{max(1, -(-len(items) // PAGE))}")
    markup = ui.kb(*rows)
    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=markup)
            return
        except Exception:
            pass
    await ui.reply(update, text, markup)


@require_user
async def cb_pick(update, ctx, user):
    await ui.answer(update)
    course = await require_course(update, ctx, user)
    if not course:
        return
    op = update.callback_query.data
    async with ui.sf(ctx)() as s:
        st = await repo.get_state(s, user.id)
        data = dict(st.data or {"sel": [], "page": 0})
        items = await repo.load_plan(s, course.id)
        sel, page = list(data.get("sel", [])), data.get("page", 0)
        if op.startswith("gt:"):
            i = int(op[3:])
            sel = [x for x in sel if x != i] if i in sel else sel + [i]
        elif op.startswith("gp:"):
            page = int(op[3:])
        elif op == "gx":
            await repo.set_state(s, user.id, mode=None, data={})
            await ui.reply(update, "Cancelled.")
            return
        elif op == "gg":
            if not sel:
                await ui.reply(update, "Nothing selected.")
                return
        await repo.set_state(s, user.id, mode="pick", data={"sel": sel, "page": page})
    if op == "gg":
        await start_job(update, ctx, user, course, sel)
    else:
        await show_pick(update, ctx, items, sel, page)


async def on_pick_text(update, ctx, user, text):
    course = await require_course(update, ctx, user)
    if not course:
        return
    async with ui.sf(ctx)() as s:
        items = await repo.load_plan(s, course.id)
    sel, problems = parse_selection(text, items)
    if problems:
        await ui.reply(update, "Problems: " + "; ".join(problems) + (". Selected the rest." if sel else ". Try again."))
    if sel:
        await start_job(update, ctx, user, course, [i.id for i in sel])


@require_user
async def cb_retry_failed(update, ctx, user):
    await ui.answer(update)
    course = await require_course(update, ctx, user)
    if not course:
        return
    job_id = int(update.callback_query.data.split(":")[1])
    async with ui.sf(ctx)() as s:
        job = await s.get(Job, job_id)
        ids = [int(k) for k in (job.payload or {}).get("failed", {})] if job else []
        ids = [i for i in ids if (await s.get(PlanItem, i)) is not None]
    await start_job(update, ctx, user, course, ids)


@require_user
async def cmd_cancel(update, ctx, user):
    async with ui.sf(ctx)() as s:
        res = await s.execute(sa_update(Job).where(Job.created_by == user.id, Job.status.in_(["queued", "running"])).values(status="cancelled"))
        await s.commit()
    await ui.reply(update, "Cancelled your job(s); the current item finishes first." if res.rowcount else "You have no running job.")
