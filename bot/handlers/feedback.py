"""Feedback capture, rule extraction/display/editing, idle sweep (spec 6.5, 6.6)."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from telegram import Update
from telegram.ext import ContextTypes

from bot.db.models import Course, FeedbackMessage, FeedbackRule, PlanItem, UserState
from bot.handlers import ui
from bot.handlers.auth import require_user
from bot.handlers.courses import require_course
from bot.services import feedback_store as fb, repo
from bot.services.llm_router import AllProvidersBusy

log = logging.getLogger("feedback")
IDLE_TTL_S = 120


async def start_feedback(update: Update, ctx, user, item_id: int, first_text: str | None = None):
    async with ui.sf(ctx)() as s:
        item = await s.get(PlanItem, item_id)
        if not item:
            await ui.reply(update, "Item not found.")
            return
        data = {"item_id": item_id, "texts": [first_text] if first_text else [], "chat_id": update.effective_chat.id}
        await repo.set_state(s, user.id, mode="feedback", course_id=item.course_id, data=data)
    warn = " It is currently approved; feedback will send it back to draft." if item.status == "approved" else ""
    await ui.reply(update, (f"Feedback mode for {item.type} {item.seq} - {item.title}.{warn}\n" if not first_text else "Got it.\n") +
                   "Send your feedback as one or more messages, then tap Done (or wait ~2 minutes).",
                   ui.kb([("Done", "fd")]))


@require_user
async def cb_feedback_start(update, ctx, user):
    await ui.answer(update)
    await start_feedback(update, ctx, user, int(update.callback_query.data.split(":")[1]))


async def on_feedback_text(update: Update, ctx, user, text: str):
    async with ui.sf(ctx)() as s:
        st = await repo.get_state(s, user.id)
        data = dict(st.data or {})
        data["texts"] = [*data.get("texts", []), text]
        await repo.set_state(s, user.id, data=data)
    await ui.reply(update, f"Noted ({len(data['texts'])}). Send more or tap Done.", ui.kb([("Done", "fd")]))


@require_user
async def cb_feedback_done(update, ctx, user):
    await ui.answer(update)
    await finish_feedback(ctx.application, user.id, update.effective_chat.id)


def _rules_markup(rules, extra=None):
    rows = [[(f"Flip scope #{r.id}", f"rl:{r.id}:flip"), (f"Delete #{r.id}", f"rl:{r.id}:del")] for r in rules]
    return ui.kb(*rows)


async def finish_feedback(app, user_id: int, chat_id: int):
    """Process collected feedback: extract rules, resolve conflicts, queue a new version. Safe to call twice."""
    sf, llm, bot = app.bot_data["db"], app.bot_data["llm"], app.bot
    async with sf() as s:
        st = await repo.get_state(s, user_id)
        if st.mode != "feedback":
            return
        data = dict(st.data or {})
        item_id, texts = data.get("item_id"), data.get("texts", [])
        await repo.set_state(s, user_id, mode=None, data={})  # close input first: prevents double processing
    if not texts or not item_id:
        await bot.send_message(chat_id, "No feedback received; nothing changed.")
        return
    try:
        async with sf() as s:
            item = await s.get(PlanItem, item_id)
            was_approved = item.status == "approved"
            desc = f"{item.type} {item.seq} - {item.title} (week {item.week})"
            out = await fb.process_feedback(s, llm, course_id=item.course_id, item_id=item_id, user_id=user_id,
                                            raw_texts=texts, item_desc=desc)
            rules = out.rules
            lines = []
            for r in rules:
                lines.append(("Stored as course rule: " if r.item_id is None else "Pinned to this item: ") + r.rule_text)
            for old, new in out.superseded:
                lines.append(f"Replaced older rule: \"{old.rule_text}\" -> now \"{new.rule_text}\"")
            for old, new in out.overrides:
                lines.append(f"Note: for this item only, \"{new.rule_text}\" overrides course rule \"{old.rule_text}\".")
            if was_approved:
                lines.append("This item was approved; the revised version goes back to draft for re-approval.")
            course = await s.get(Course, item.course_id)
    except AllProvidersBusy as e:  # keep the user's text so Done can be pressed again
        async with sf() as s:
            await repo.set_state(s, user_id, mode="feedback", data={"item_id": item_id, "texts": texts, "chat_id": chat_id})
        await bot.send_message(chat_id, f"AI providers are rate-limited. Your feedback is kept - tap Done again in ~{int(e.retry_after)} s.",
                               reply_markup=ui.kb([("Done", "fd")]))
        return
    except ValueError:
        async with sf() as s:
            await repo.set_state(s, user_id, mode="feedback", data={"item_id": item_id, "texts": texts, "chat_id": chat_id})
        await bot.send_message(chat_id, "I couldn't interpret that feedback. Rephrase it and tap Done.", reply_markup=ui.kb([("Done", "fd")]))
        return
    if not rules:
        await bot.send_message(chat_id, "I found no actionable rule in that feedback; nothing changed.")
        return
    await bot.send_message(chat_id, "\n".join(lines), reply_markup=_rules_markup(rules))
    pm = await bot.send_message(chat_id, "Regenerating this item with the updated rules...")
    async with sf() as s:
        await repo.create_job(s, item.course_id, "generate", {"item_ids": [item_id], "chat_id": chat_id,
                                                              "progress_msg_id": pm.message_id, "origin": "feedback"}, user_id)


async def sweep_idle(app):
    """Worker tick: close feedback input that has been idle > IDLE_TTL_S (spec 6.5 idle timeout)."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=IDLE_TTL_S)
    async with app.bot_data["db"]() as s:
        stale = [(st.user_id, (st.data or {}).get("chat_id")) for st in
                 (await s.execute(select(UserState).where(UserState.mode == "feedback"))).scalars()
                 if ui.aware(st.updated_at) < cutoff and (st.data or {}).get("texts")]
    for uid, chat in stale:
        if chat:
            await finish_feedback(app, uid, chat)


# ---- rules --------------------------------------------------------------
async def show_rules(update, ctx, user):
    course = await require_course(update, ctx, user)
    if not course:
        return
    async with ui.sf(ctx)() as s:
        crules, _ = await fb.active_rules(s, course.id)
        pinned = list((await s.execute(select(FeedbackRule).where(
            FeedbackRule.course_id == course.id, FeedbackRule.active.is_(True), FeedbackRule.item_id.is_not(None)))).scalars())
    lines = ["Course rules (apply to every generation):"] + [f"#{r.id}: {r.rule_text}" for r in crules] if crules else ["No course rules yet."]
    if pinned:
        lines += ["", "Item-pinned rules:"] + [f"#{r.id} (item {r.item_id}): {r.rule_text}" for r in pinned]
    rows = [[(f"Edit #{r.id}", f"rl:{r.id}:edit"), (f"Delete #{r.id}", f"rl:{r.id}:del")] for r in [*crules, *pinned][:30]]
    await ui.reply(update, "\n".join(lines), ui.kb(*rows))


@require_user
async def cmd_rules(update, ctx, user):
    await ui.answer(update)
    await show_rules(update, ctx, user)


@require_user
async def cb_rule(update, ctx, user):
    await ui.answer(update)
    _, rid, op = update.callback_query.data.split(":")
    rid = int(rid)
    async with ui.sf(ctx)() as s:
        r = await s.get(FeedbackRule, rid)
        if not r:
            await ui.reply(update, "Rule not found.")
            return
        if op == "del":
            await fb.deactivate(s, rid)
            await ui.reply(update, f"Deleted rule #{rid}. Existing items are unchanged; future generations won't use it.")
        elif op == "flip":
            if r.item_id is None:
                msg = await s.get(FeedbackMessage, r.source_message_id) if r.source_message_id else None
                if not msg or not msg.item_id:
                    await ui.reply(update, "I don't know which item this was pinned to, so it can't be flipped to item scope.")
                    return
                await fb.set_scope(s, rid, msg.item_id)
                await ui.reply(update, f"Rule #{rid} is now pinned to that item only.")
            else:
                await fb.set_scope(s, rid, None)
                await ui.reply(update, f"Rule #{rid} is now a course rule.")
        elif op == "edit":
            await repo.set_state(s, user.id, mode="rule_edit", data={"rule_id": rid})
            await ui.reply(update, f"Send the new text for rule #{rid}:\n{r.rule_text}")


async def on_rule_edit_text(update, ctx, user, text):
    async with ui.sf(ctx)() as s:
        st = await repo.get_state(s, user.id)
        rid = (st.data or {}).get("rule_id")
        await fb.edit_rule(s, rid, text)
        await repo.set_state(s, user.id, mode=None, data={})
    await ui.reply(update, f"Rule #{rid} updated. It applies to future generations.")
