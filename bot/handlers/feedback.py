"""Feedback flow and conflict/scope/undo buttons (rewrite spec 6).

State machine (all state in UserState, DB-backed, restart-safe):
  start (button fb:<id> or reply-to an item message) -> ONE collecting message [Done] [Cancel]
  each text -> thumbs-up reaction + the collecting message edited ("(3 messages received)"), no reply
  Done / idle sweep -> the same message becomes the result card; regeneration is queued immediately, or only after
  the user has decided every conflict of the batch (RuleBatch.resolved flips once -> exactly one job).

Feedback on one item is a one-off fix for that item unless it explicitly generalizes; the card offers
[#41: all labs] [#41: whole course] [Undo #41] to widen or undo a rule.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from telegram import Update

from bot.db.models import FeedbackMessage, FeedbackRule, PlanItem, RuleBatch, UserState
from bot.handlers import generate, rule_cards, ui
from bot.handlers.auth import require_user
from bot.services import repo, rules
from bot.services.llm_router import AllProvidersBusy

log = logging.getLogger("feedback")
IDLE_TTL_S = 120
DONE_CANCEL = [("Done", "fd"), ("Cancel", "fx")]


def item_name(item: PlanItem) -> str:
    return f"{item.type.title()} {item.seq}"


def collecting_text(item: PlanItem, n: int) -> str:
    base = f"Feedback on {item_name(item)}. Send as many messages as you like, then tap Done."
    if item.status == "approved":
        base += " (It is approved; feedback will send it back to draft.)"
    return base + (f"\n({n} message{'s' if n != 1 else ''} received)" if n else "")


# ---- collecting ---------------------------------------------------------
async def start_feedback(update: Update, ctx, user, item_id: int, first_text: str | None = None):
    async with ui.sf(ctx)() as s:
        item = await s.get(PlanItem, item_id)
    if not item:
        await ui.reply(update, "Item not found.")
        return
    chat_id = update.effective_chat.id
    texts = [first_text] if first_text else []
    msg = await ui.reply(update, collecting_text(item, len(texts)), ui.kb(DONE_CANCEL))
    async with ui.sf(ctx)() as s:
        await repo.set_state(s, user.id, mode="feedback", course_id=item.course_id,
                             data={"item_id": item_id, "texts": texts, "chat_id": chat_id, "msg_id": msg.message_id})
    if first_text:
        await ui.react(ctx.bot, chat_id, update.effective_message.message_id)


@require_user
async def cb_feedback_start(update, ctx, user):
    await ui.answer(update)
    await start_feedback(update, ctx, user, int(update.callback_query.data.split(":")[1]))


async def on_feedback_text(update: Update, ctx, user, text: str):
    """Collect one more message: no reply, a reaction, and an in-place counter on the collecting message."""
    async with ui.sf(ctx)() as s:
        st = await repo.get_state(s, user.id)
        data = dict(st.data or {})
        data["texts"] = [*data.get("texts", []), text]
        await repo.set_state(s, user.id, data=data)
        item = await s.get(PlanItem, data.get("item_id"))
    chat_id = update.effective_chat.id
    await ui.react(ctx.bot, chat_id, update.effective_message.message_id)
    if data.get("msg_id") and item:
        await ui.live(ctx.bot_data, ctx.bot, chat_id, data["msg_id"]).update(
            collecting_text(item, len(data["texts"])), ui.kb(DONE_CANCEL))


@require_user
async def cb_feedback_done(update, ctx, user):
    await ui.answer(update)
    await finish_feedback(ctx.application, user.id, update.effective_chat.id)


@require_user
async def cb_feedback_cancel(update, ctx, user):
    await ui.answer(update)
    async with ui.sf(ctx)() as s:
        st = await repo.get_state(s, user.id)
        if st.mode == "feedback":
            await repo.set_state(s, user.id, mode=None, data={})
    await ui.edit_cb(update, "Feedback cancelled. Nothing changed.")


# ---- processing ---------------------------------------------------------
async def finish_feedback(app, user_id: int, chat_id: int):
    """Extract rules from the collected feedback, store them (conflicts become pending), show ONE card, queue the
    regeneration unless a conflict needs a decision first. Safe to call twice (state is closed first)."""
    sf, llm, bot = app.bot_data["db"], app.bot_data["llm"], app.bot
    async with sf() as s:
        st = await repo.get_state(s, user_id)
        if st.mode != "feedback":
            return
        data = dict(st.data or {})
        await repo.set_state(s, user_id, mode=None, data={})  # close input first: prevents double processing
    item_id, texts, msg_id = data.get("item_id"), data.get("texts", []), data.get("msg_id")
    if not texts or not item_id:
        await ui.edit_or_send(bot, chat_id, msg_id, "No feedback received; nothing changed.")
        return
    await ui.edit_or_send(bot, chat_id, msg_id, "Reading your feedback...")

    async def keep_and_explain(text: str):
        async with sf() as s:  # re-enter feedback mode with the same texts so Done can be tapped again
            await repo.set_state(s, user_id, mode="feedback", data=data)
        await ui.edit_or_send(bot, chat_id, msg_id, text, ui.kb(DONE_CANCEL))

    try:
        async with sf() as s:
            item = await s.get(PlanItem, item_id)
            was_approved = item.status == "approved"
            raw = "\n".join(t.strip() for t in texts if t.strip())
            fm = FeedbackMessage(course_id=item.course_id, item_id=item_id, user_id=user_id, raw_text=raw)
            s.add(fm)
            await s.flush()
            desc = f"{item.type} {item.seq} - {item.title} (week {item.week})"
            new = await rules.extract_rules(llm, raw, desc, item.type)
            if not new:
                await s.commit()  # keep the raw feedback log
                await ui.edit_or_send(bot, chat_id, msg_id, "I found no actionable rule in that feedback; nothing changed.")
                return
            batch = await rules.create_batch(s, course_id=item.course_id, user_id=user_id, kind="feedback", chat_id=chat_id,
                                             item_id=item_id, regen_item_id=item_id)
            batch.card_message_id = msg_id
            out = await rules.add_rules(s, llm, batch, new, origin="feedback", item=item, source_message_id=fm.id)
            extra = rule_cards.outcome_notes(out)
            if was_approved:
                extra.append("This item was approved; the revised version goes back to draft for re-approval.")
            text, markup = await rule_cards.render_batch_card(s, batch, extra)
    except AllProvidersBusy as e:  # nothing was stored; keep the user's text so Done can be pressed again
        await keep_and_explain(f"AI providers are rate-limited. Your feedback is kept - tap Done again in ~{int(e.retry_after)} s.")
        return
    except ValueError:
        await keep_and_explain("I couldn't interpret that feedback. Send a rephrased message (or tap Done to retry).")
        return
    except Exception:
        log.exception("feedback processing failed")
        await keep_and_explain("Something went wrong while saving your feedback. Your messages are kept; tap Done to retry.")
        return
    used = await ui.edit_or_send(bot, chat_id, msg_id, text, markup)
    if used != msg_id:
        async with sf() as s:
            (await s.get(RuleBatch, batch.id)).card_message_id = used
            await s.commit()
    if out.resolved_batch is not None:
        await queue_regen(app, batch)


async def queue_regen(app, batch: RuleBatch):
    """Queue the from-scratch regeneration of a resolved feedback batch (ONE status message + ONE job)."""
    sf, bot = app.bot_data["db"], app.bot
    if not batch.regen_item_id:
        return
    msg = await bot.send_message(batch.chat_id, await generate.initial_status_text(sf, [batch.regen_item_id]))
    async with sf() as s:
        await repo.queue_regen(s, batch, status_msg_id=msg.message_id)


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


# ---- card buttons: conflicts, scope, undo --------------------------------
async def render_for_message(s, rule: FeedbackRule, msg_id: int | None):
    """Batch card if `msg_id` is that batch's card, else the pending-decisions page."""
    batch = await s.get(RuleBatch, rule.batch_id) if rule.batch_id else None
    if batch is not None and batch.card_message_id == msg_id:
        return await rule_cards.render_batch_card(s, batch)
    return await rule_cards.render_pending_page(s, rule.course_id)


async def after_resolution(app, batch: RuleBatch | None):
    """Queue the regeneration once, the moment a feedback batch becomes fully resolved."""
    if batch is not None and batch.kind == "feedback" and batch.regen_item_id:
        await queue_regen(app, batch)


@require_user
async def cb_conflict(update, ctx, user):
    _, rid, choice = update.callback_query.data.split(":")
    rid = int(rid)
    msg_id = update.callback_query.message.message_id
    async with ui.sf(ctx)() as s:
        r = await s.get(FeedbackRule, rid)
        if r is None:
            await ui.answer(update, "Rule not found.")
            return
        was_pending = r.status == "pending" and r.pending_reason == "conflict"
        if choice == "edit":
            if not was_pending:
                await ui.answer(update, "Already decided.")
                return
            await repo.set_state(s, user.id, mode="rule_conflict_edit",
                                 data={"rule_id": rid, "msg_id": msg_id, "chat_id": update.effective_chat.id})
            await ui.answer(update)
            await ui.edit_cb(update, f'Editing #{rid}. Send the new text for this rule as your next message.\nCurrent: "{r.rule_text}"',
                             ui.kb([("Back", f"cf:{rid}:back")]))
            return
        batch = None
        if choice == "back":
            await repo.set_state(s, user.id, mode=None, data={})
        else:
            batch = await rules.resolve_conflict(s, rid, choice)  # None for an already-decided rule (double tap)
            await s.refresh(r)
        text, markup = await render_for_message(s, r, msg_id)
    await ui.answer(update, None if was_pending or choice == "back" else "Already decided.")
    await ui.edit_cb(update, text, markup)
    await after_resolution(ctx.application, batch)


async def on_conflict_edit_text(update, ctx, user, text: str):
    """The user replaced the text of a pending rule: re-check it, refresh the card, maybe queue regeneration."""
    async with ui.sf(ctx)() as s:
        st = await repo.get_state(s, user.id)
        data = dict(st.data or {})
    rid, msg_id, chat_id = data.get("rule_id"), data.get("msg_id"), data.get("chat_id") or update.effective_chat.id
    try:
        async with ui.sf(ctx)() as s:
            out = await rules.edit_rule_text(s, ui.llm(ctx), rid, text)
    except AllProvidersBusy as e:
        await ui.reply(update, f"AI providers are rate-limited; send the new text again in ~{int(e.retry_after)} s.")
        return
    except (KeyError, ValueError):
        await ui.reply(update, "I couldn't apply that edit. Send the new rule text again.")
        return
    async with ui.sf(ctx)() as s:
        await repo.set_state(s, user.id, mode=None, data={})
        r = await s.get(FeedbackRule, rid)
        card, markup = await render_for_message(s, r, msg_id)
    await ui.react(ctx.bot, chat_id, update.effective_message.message_id)
    await ui.edit_or_send(ctx.bot, chat_id, msg_id, card, markup)
    await after_resolution(ctx.application, out.resolved_batch)


@require_user
async def cb_scope(update, ctx, user):
    """Widen/narrow a rule: sc:<id>:<type|course|lab|tutorial|assignment|item> from a card, or sh:<...> from the
    /rules hub (which returns to the hub afterwards). Re-checks conflicts."""
    prefix, rid, opt = update.callback_query.data.split(":")
    from_hub = prefix == "sh"
    rid = int(rid)
    msg_id = update.callback_query.message.message_id
    async with ui.sf(ctx)() as s:
        r = await s.get(FeedbackRule, rid)
        if r is None:
            await ui.answer(update, "Rule not found.")
            return
        try:
            if opt == "course":
                args = ("course", None, None)
            elif opt == "item":
                fm = await s.get(FeedbackMessage, r.source_message_id) if r.source_message_id else None
                if not (fm and fm.item_id):
                    raise ValueError("I don't know which item this rule came from.")
                args = ("item", None, fm.item_id)
            else:
                itype = opt if opt in rules.ITEM_TYPES else r.item_type
                if opt == "type" and not itype and r.item_id:
                    it = await s.get(PlanItem, r.item_id)
                    itype = it.type if it else None
                if not itype:
                    raise ValueError("Pick a specific type for this rule in /rules > Scope.")
                args = ("type", itype, None)
            out = await rules.change_scope(s, ui.llm(ctx), rid, args[0], item_type=args[1], item_id=args[2])
        except AllProvidersBusy as e:
            await ui.answer(update, f"AI providers are rate-limited; try again in ~{int(e.retry_after)} s.")
            return
        except ValueError as e:
            await ui.answer(update, str(e)[:190])
            return
        await s.refresh(r)
        where = {"course": "the whole course", "type": f"all {rule_cards.TYPE_PLURAL.get(r.item_type, '')}", "item": "this item only"}[r.scope]
        toast = f"#{rid} now applies to {where} (from the next generation)." if r.status == "active" else None
        text, markup = await (rule_cards.render_pending_page(s, r.course_id) if from_hub and r.status == "pending"
                              else render_for_message(s, r, msg_id))
    await ui.answer(update, toast)
    if from_hub and r.status != "pending":
        from bot.handlers import rules_hub
        await rules_hub.show_hub(update, ctx, user, edit=True)
    else:
        await ui.edit_cb(update, text, markup)
    await after_resolution(ctx.application, out.resolved_batch)


@require_user
async def cb_undo(update, ctx, user):
    """Undo = disable (reversible from /rules > Disabled), never a hard delete."""
    rid = int(update.callback_query.data.split(":")[1])
    msg_id = update.callback_query.message.message_id
    async with ui.sf(ctx)() as s:
        r = await s.get(FeedbackRule, rid)
        if r is None:
            await ui.answer(update, "Rule not found.")
            return
        batch = await rules.disable_rule(s, rid, "undone by you")
        await s.refresh(r)
        text, markup = await render_for_message(s, r, msg_id)
    await ui.answer(update, f"Undone #{rid}. Versions already generated keep their content; restore it in /rules > Disabled.")
    await ui.edit_cb(update, text, markup)
    await after_resolution(ctx.application, batch)
