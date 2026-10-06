"""Plan building, review, free-text edit, confirmation (spec 6.3)."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from bot.db.models import Course
from bot.handlers import ui
from bot.handlers.auth import require_user
from bot.handlers.courses import require_course
from bot.services import planner, repo
from bot.services.llm_router import AllProvidersBusy
from bot.services.outline_parser import Outline


async def show_plan(update: Update, ctx, course: Course, header: str = ""):
    async with ui.sf(ctx)() as s:
        drafts = repo.to_drafts(await repo.load_plan(s, course.id))
    inferred = sum(1 for d in drafts if d.source == "inferred")
    note = (f"\n{inferred} item(s) are [inferred]: the outline did not list them, so I proposed them. Edit freely."
            if inferred else "")
    chunks = planner.format_plan(drafts)
    for c in chunks[:-1]:
        await ui.reply(update, c)
    buttons = [[("Edit plan", "pe")]] + ([[("Generate", "m:gen")]] if course.plan_confirmed else [[("Confirm plan", "pc")]])
    await ui.reply(update, (header + "\n" if header else "") + chunks[-1] + note, ui.kb(*buttons))


async def start_plan(update: Update, ctx, user, course: Course):
    """Build from the outline if no plan exists yet, then show it for confirmation."""
    async with ui.sf(ctx)() as s:
        c = await s.get(Course, course.id)
        if not await repo.load_plan(s, c.id):
            await repo.save_plan(s, c.id, planner.build_plan(Outline.model_validate(c.outline_json or {})))
        await repo.set_state(s, user.id, mode="plan_review", course_id=c.id, data={})
    await show_plan(update, ctx, c, "Here is the proposed term plan. Nothing is generated until you confirm.")


@require_user
async def cb_outline_continue(update: Update, ctx, user):
    await ui.answer(update)
    course = await require_course(update, ctx, user)
    if course:
        await start_plan(update, ctx, user, course)


@require_user
async def cmd_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user):
    await ui.answer(update)
    course = await require_course(update, ctx, user)
    if course:
        await start_plan(update, ctx, user, course) if not course.plan_confirmed else await show_plan(update, ctx, course)


@require_user
async def cb_confirm(update, ctx, user):
    await ui.answer(update)
    course = await require_course(update, ctx, user)
    if not course:
        return
    async with ui.sf(ctx)() as s:
        (await s.get(Course, course.id)).plan_confirmed = True
        await repo.set_state(s, user.id, mode=None)
    from bot.handlers.rules_hub import send_gate
    await send_gate(update, ctx, user, course)


@require_user
async def cb_edit(update, ctx, user):
    await ui.answer(update)
    if not await require_course(update, ctx, user):
        return
    async with ui.sf(ctx)() as s:
        await repo.set_state(s, user.id, mode="plan_edit")
    await ui.reply(update, "Describe the changes in plain text, e.g. \"drop lab 5, add a tutorial for week 6 on AVL trees, "
                           "move assignment 2 to week 7\". Items are numbered per type as shown.")


async def on_plan_edit_text(update: Update, ctx, user, text: str):
    course = await require_course(update, ctx, user)
    if not course:
        return
    async with ui.sf(ctx)() as s:
        drafts = repo.to_drafts(await repo.load_plan(s, course.id))
    try:
        ops = await planner.edit_ops_from_text(ui.llm(ctx), drafts, text)
    except AllProvidersBusy as e:
        await ui.reply(update, f"AI providers are rate-limited; send your edit again in ~{int(e.retry_after)} s.")
        return
    except ValueError:
        await ui.reply(update, "I couldn't turn that into plan changes. Try rephrasing, e.g. \"remove lab 3\".")
        return
    new, log = planner.apply_ops(drafts, ops)
    async with ui.sf(ctx)() as s:
        await repo.save_plan(s, course.id, new)
        await repo.set_state(s, user.id, mode="plan_review")
        fresh = await s.get(Course, course.id)
    await show_plan(update, ctx, fresh, "Applied:\n- " + "\n- ".join(log or ["(no changes)"]))
