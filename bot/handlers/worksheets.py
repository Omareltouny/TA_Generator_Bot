"""Worksheet format spec (build -> review -> approve) and the style-examples page (rewrite spec 7.1, 8).

Callback data (<= 64 bytes; batch ids are 36-char uuids):
  ws:menu | ws:build:<type> | ws:rev:<batch> | ws:ok|rp|kb|cx|ed|dl|bk:<batch> | ws:hw:<batch>:<word> | ws:e|d:<rule_id>
  r:ex | r:xt:<material_id>
Nothing becomes active before "Approve all"; replacing an earlier spec is an explicit button.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from telegram import Update

from bot.db.models import Material, RuleBatch
from bot.handlers import rule_cards, ui
from bot.handlers.auth import require_user
from bot.handlers.courses import require_course
from bot.services import repo, rules, worksheets as ws
from bot.services.generator import HEADING_WORDS
from bot.services.llm_router import AllProvidersBusy

log = logging.getLogger("worksheets")
PLURAL = rule_cards.TYPE_PLURAL


# ---- review message ----------------------------------------------------------
async def render_review(s, batch: RuleBatch):
    rows = await ws.spec_rules(s, batch.id)
    t = batch.item_type
    lines = [f"Proposed format for {PLURAL[t]}, extracted from your worksheets. Heading word: {batch.heading_word}", ""]
    for aspect, rs in ws.group_by_aspect(rows):
        lines.append(f"{aspect}:")
        lines += [f"  #{r.id} {r.rule_text}" for r in rs]
    if not rows:
        lines.append("No rules are left in this proposal.")
    lines += ["", f"On approval these become rules for all {PLURAL[t]} (editable later in /rules). Nothing changes until you approve."]
    hw_row = [((f"*{w}" if w == batch.heading_word else w), f"ws:hw:{batch.id}:{w}") for w in HEADING_WORDS]
    kb = [hw_row]
    if rows:
        kb += [[("Approve all", f"ws:ok:{batch.id}"), ("Edit...", f"ws:ed:{batch.id}"), ("Delete...", f"ws:dl:{batch.id}")]]
    kb += [[("Cancel", f"ws:cx:{batch.id}")]]
    return "\n".join(lines), ui.kb(*kb)


async def refresh_review(app, batch_id: str, chat_id: int, msg_id: int | None) -> None:
    async with app.bot_data["db"]() as s:
        batch = await s.get(RuleBatch, batch_id)
        text, markup = await render_review(s, batch)
    await ui.edit_or_send(app.bot, chat_id, msg_id, text, markup)


async def _cb_batch(update, ctx):
    bid = update.callback_query.data.split(":")[2]
    async with ui.sf(ctx)() as s:
        return bid, await s.get(RuleBatch, bid)


# ---- build -------------------------------------------------------------------
@require_user
async def cb_menu(update, ctx, user):
    await ui.answer(update)
    course = await require_course(update, ctx, user)
    if not course:
        return
    async with ui.sf(ctx)() as s:
        by = await ws.worksheets_by_type(s, course.id)
    if not any(by.values()):
        await ui.reply(update, "No past worksheets yet. Upload PDFs of earlier labs, tutorials or assignments first.",
                       ui.kb([("Upload worksheets", "up:reference")]))
        return
    rows = [[(f"{t.title()}s ({len(by[t])} worksheet{'s' if len(by[t]) != 1 else ''})", f"ws:build:{t}")] for t in rules.ITEM_TYPES if by[t]]
    await ui.reply(update, "Build a format spec from which worksheets?", ui.kb(*rows, [("Upload more", "up:reference")]))


@require_user
async def cb_build(update, ctx, user):
    await ui.answer(update)
    course = await require_course(update, ctx, user)
    if not course:
        return
    t = update.callback_query.data.split(":")[2]
    chat_id = update.effective_chat.id
    async with ui.sf(ctx)() as s:
        open_batch = await ws.open_spec_batch(s, course.id, t)
        n = len(await repo.reference_materials(s, course.id, t))
        if open_batch:
            text, markup = await render_review(s, open_batch)
    if open_batch:
        msg = await ui.reply(update, "A proposal for this type is already waiting for your review:\n\n" + text, markup)
        async with ui.sf(ctx)() as s:
            (await s.get(RuleBatch, open_batch.id)).card_message_id = msg.message_id
            await s.commit()
        return
    if not n:
        await ui.reply(update, f"No {t} worksheets uploaded yet.", ui.kb([("Upload worksheets", "up:reference")]))
        return
    status = await ui.reply(update, f"Reading {min(n, ws.SPEC_MAX_WORKSHEETS)} {t} worksheet(s) and extracting their format...")
    try:
        async with ui.sf(ctx)() as s:
            batch = await ws.build_format_spec(s, ui.llm(ctx), course_id=course.id, user_id=user.id, chat_id=chat_id, item_type=t)
            batch.card_message_id = status.message_id
            await s.commit()
            text, markup = await render_review(s, batch)
    except AllProvidersBusy as e:
        await ui.edit_or_send(ctx.bot, chat_id, status.message_id,
                              f"AI providers are rate-limited. Tap Build format spec again in ~{int(e.retry_after)} s.", ui.kb([("Try again", f"ws:build:{t}")]))
        return
    except ValueError as e:
        await ui.edit_or_send(ctx.bot, chat_id, status.message_id, f"I couldn't extract a format ({str(e)[:150]}).")
        return
    await ui.edit_or_send(ctx.bot, chat_id, status.message_id, text, markup)


@require_user
async def cb_review(update, ctx, user):
    """ws:rev:<batch> - reopen a proposal (from the pending-decisions page)."""
    await ui.answer(update)
    _, batch = await _cb_batch(update, ctx)
    if batch is None:
        return
    async with ui.sf(ctx)() as s:
        text, markup = await render_review(s, await s.get(RuleBatch, batch.id))
    msg = await ui.reply(update, text, markup)
    async with ui.sf(ctx)() as s:
        (await s.get(RuleBatch, batch.id)).card_message_id = msg.message_id
        await s.commit()


# ---- review actions ----------------------------------------------------------
@require_user
async def cb_heading(update, ctx, user):
    _, _, bid, word = update.callback_query.data.split(":")
    async with ui.sf(ctx)() as s:
        batch = await s.get(RuleBatch, bid)
        if batch is None or batch.resolved:
            await ui.answer(update, "This proposal is closed.")
            return
        await ws.set_proposed_heading(s, batch, word)
        text, markup = await render_review(s, batch)
    await ui.answer(update, f"Heading word: {word}")
    await ui.edit_cb(update, text, markup)


@require_user
async def cb_pick_rules(update, ctx, user):
    """ws:ed:<batch> / ws:dl:<batch>: show one button per rule to edit or delete."""
    await ui.answer(update)
    op, bid = update.callback_query.data.split(":")[1:3]
    async with ui.sf(ctx)() as s:
        batch = await s.get(RuleBatch, bid)
        rows = await ws.spec_rules(s, bid)
    verb, cb = ("Edit", "e") if op == "ed" else ("Delete", "d")
    kb = [[(f"{verb} #{r.id}: {r.rule_text[:40]}", f"ws:{cb}:{r.id}")] for r in rows] + [[("Back", f"ws:bk:{bid}")]]
    await ui.edit_cb(update, f"Which rule do you want to {verb.lower()}?", ui.kb(*kb))


@require_user
async def cb_back(update, ctx, user):
    await ui.answer(update)
    bid = update.callback_query.data.split(":")[2]
    async with ui.sf(ctx)() as s:
        text, markup = await render_review(s, await s.get(RuleBatch, bid))
    await ui.edit_cb(update, text, markup)


@require_user
async def cb_rule_edit(update, ctx, user):
    """ws:e:<rule_id>: the next text message replaces this proposed rule."""
    await ui.answer(update)
    rid = int(update.callback_query.data.split(":")[2])
    from bot.db.models import FeedbackRule
    async with ui.sf(ctx)() as s:
        r = await s.get(FeedbackRule, rid)
        if r is None or r.status != "pending":
            return
        await repo.set_state(s, user.id, mode="rule_edit", data={"rule_id": rid, "batch": r.batch_id,
                                                                "msg_id": update.callback_query.message.message_id,
                                                                "chat_id": update.effective_chat.id})
        text = r.rule_text
    await ui.edit_cb(update, f"Send the new text for #{rid}:\n{text}", ui.kb([("Back", f"ws:bk:{r.batch_id}")]))


@require_user
async def cb_rule_delete(update, ctx, user):
    """ws:d:<rule_id>: drop one rule from the (never active) proposal."""
    rid = int(update.callback_query.data.split(":")[2])
    from bot.db.models import FeedbackRule
    async with ui.sf(ctx)() as s:
        r = await s.get(FeedbackRule, rid)
        if r is None or r.status != "pending":
            await ui.answer(update, "Already gone.")
            return
        bid = r.batch_id
        await rules.delete_rule_permanently(s, rid)
        text, markup = await render_review(s, await s.get(RuleBatch, bid))
    await ui.answer(update, f"#{rid} removed from the proposal.")
    await ui.edit_cb(update, text, markup)


@require_user
async def cb_cancel(update, ctx, user):
    await ui.answer(update)
    _, batch = await _cb_batch(update, ctx)
    if batch is None:
        return
    async with ui.sf(ctx)() as s:
        await ws.cancel_spec(s, await s.get(RuleBatch, batch.id))
    await ui.edit_cb(update, "Proposal cancelled. No rules were added and nothing changed.")


async def _approve(update, ctx, user, bid: str, replace: bool):
    msg_id = update.callback_query.message.message_id
    try:
        async with ui.sf(ctx)() as s:
            batch = await s.get(RuleBatch, bid)
            if batch is None or batch.resolved or not await ws.spec_rules(s, bid):
                await ui.answer(update, "Nothing to approve here.")
                return
            if replace:
                await ws.replace_previous_spec(s, batch.course_id, batch.item_type, bid)
            out = await ws.approve_spec(s, ui.llm(ctx), batch)
            batch.card_message_id = msg_id
            await s.commit()
            text, markup = await rule_cards.render_batch_card(
                s, batch, [f"Heading word for {PLURAL[batch.item_type]} is now {batch.heading_word}."] + rule_cards.outcome_notes(out))
    except AllProvidersBusy as e:
        await ui.answer(update, f"AI providers are rate-limited; tap Approve again in ~{int(e.retry_after)} s. Nothing changed.")
        return
    await ui.answer(update)
    await ui.edit_cb(update, text, markup)


@require_user
async def cb_approve(update, ctx, user):
    bid = update.callback_query.data.split(":")[2]
    async with ui.sf(ctx)() as s:
        batch = await s.get(RuleBatch, bid)
        prev = await ws.previous_worksheet_rules(s, batch.course_id, batch.item_type, bid) if batch else []
    if prev:
        await ui.answer(update)
        await ui.edit_cb(update, f"An earlier worksheet format for {PLURAL[batch.item_type]} is active ({len(prev)} rule(s)). "
                                 "Replace it with the new one, or keep both?",
                         ui.kb([("Replace previous spec", f"ws:rp:{bid}"), ("Keep both", f"ws:kb:{bid}")], [("Back", f"ws:bk:{bid}")]))
        return
    await _approve(update, ctx, user, bid, replace=False)


@require_user
async def cb_approve_choice(update, ctx, user):
    op, bid = update.callback_query.data.split(":")[1:3]
    await _approve(update, ctx, user, bid, replace=(op == "rp"))


# ---- style examples page -------------------------------------------------------
async def show_examples(update, ctx, user, edit: bool = False):
    course = await require_course(update, ctx, user)
    if not course:
        return
    limit = ui.cfg(ctx).examples_per_type
    async with ui.sf(ctx)() as s:
        by = await ws.worksheets_by_type(s, course.id)
    lines = [f"Style examples: up to {limit} worksheets per type are attached to prompts ([x] = attached). "
             "Tap one to attach or detach it."]
    rows = []
    for t in rules.ITEM_TYPES:
        if not by[t]:
            continue
        lines.append(f"{t.title()}s: {sum(1 for m in by[t] if m.is_example)} of {limit} attached")
        rows += [[(("[x] " if m.is_example else "[ ] ") + m.filename[:50], f"r:xt:{m.id}")] for m in by[t]]
    if not rows:
        lines.append("No worksheets uploaded yet.")
    rows += [[("Back", "r:hub")]]
    if edit and update.callback_query:
        await ui.edit_cb(update, "\n".join(lines), ui.kb(*rows))
    else:
        await ui.reply(update, "\n".join(lines), ui.kb(*rows))


@require_user
async def cb_examples(update, ctx, user):
    await ui.answer(update)
    await show_examples(update, ctx, user, edit=True)


@require_user
async def cb_example_toggle(update, ctx, user):
    mid = int(update.callback_query.data.split(":")[2])
    async with ui.sf(ctx)() as s:
        m, swapped = await ws.toggle_example(s, mid, ui.cfg(ctx).examples_per_type)
    if m is None:
        await ui.answer(update, "File not found.")
        return
    await ui.answer(update, (f"Attached. {swapped.filename} was detached (limit reached)." if swapped else
                             ("Attached as a style example." if m.is_example else "Detached.")))
    await show_examples(update, ctx, user, edit=True)
