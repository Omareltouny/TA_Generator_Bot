"""Course creation entry, listing, selection and the per-course menu."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from bot.db.models import Course, PlanItem
from bot.handlers import ui
from bot.handlers.auth import require_user
from bot.services import repo
from sqlalchemy import func, select


@require_user
async def cmd_newcourse(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user):
    async with ui.sf(ctx)() as s:
        await repo.set_state(s, user.id, mode="awaiting_outline", course_id=None, data={})
    await ui.answer(update)
    await ui.reply(update, "Send the course outline as a PDF (required). Afterwards you can add slides, past labs/tutorials "
                           "(files or one zip) and a logo.")


def course_title(c: Course) -> str:
    return f"{c.code or '?'} - {c.name or '(unnamed)'}" + (f" ({c.term})" if c.term else "")


@require_user
async def cmd_courses(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user):
    async with ui.sf(ctx)() as s:
        courses = await repo.list_courses(s)
    await ui.answer(update)
    rows = [[(course_title(c)[:60], f"sel:{c.id}")] for c in courses[:40]] + [[("New course", "nc")]]
    await ui.reply(update, "Courses (shared by all TAs):" if courses else "No courses yet.", ui.kb(*rows))


async def get_course(ctx, user) -> Course | None:
    async with ui.sf(ctx)() as s:
        st = await repo.get_state(s, user.id)
        await s.commit()
        return await s.get(Course, st.course_id) if st.course_id else None


async def require_course(update, ctx, user) -> Course | None:
    c = await get_course(ctx, user)
    if c is None:
        await ui.reply(update, "No course selected. Use /courses or /newcourse.")
    return c


async def show_course_menu(update: Update, ctx, course: Course):
    async with ui.sf(ctx)() as s:
        rows = (await s.execute(select(PlanItem.status, func.count()).where(PlanItem.course_id == course.id)
                                .group_by(PlanItem.status))).all()
        n_rules = await repo.active_rules_count(s, course.id)
    counts = ", ".join(f"{n} {st}" for st, n in rows) or "no plan yet"
    await ui.reply(update, f"{course_title(course)}\nItems: {counts}\nActive course rules: {n_rules}\n"
                           f"Plan confirmed: {'yes' if course.plan_confirmed else 'no'}",
                   ui.kb([("Items", "m:items"), ("Plan", "m:plan")], [("Generate", "m:gen"), ("Rules", "m:rules")],
                         [("Add material / logo", "m:up"), ("Template", "m:tpl")]))


@require_user
async def cb_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user):
    await ui.answer(update)
    cid = int(update.callback_query.data.split(":")[1])
    async with ui.sf(ctx)() as s:
        course = await s.get(Course, cid)
        if course is None:
            await ui.reply(update, "Course not found.")
            return
        await repo.set_state(s, user.id, mode=None, course_id=cid, data={})
    await show_course_menu(update, ctx, course)
