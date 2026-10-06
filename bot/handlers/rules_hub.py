"""/rules hub, add-rule flow, pending decisions and the pre-generation gate (rewrite spec 7).

Callback data (all <= 64 bytes):
  r:hub | r:g:<group>:<page> | r:e|d|u|x|xx|s:<rule_id>[:<group>:<page>] | r:add | r:as:<scope> | r:ad | r:ax | r:pend
Groups: course | lab | tutorial | assignment | item | disabled.
Nothing here deactivates a rule except the buttons the user taps (Disable, Undo, a conflict decision).
"""
from __future__ import annotations

from sqlalchemy import select
from telegram import Update

from bot.db.models import FeedbackMessage, FeedbackRule, RuleBatch
from bot.handlers import feedback as fbh, rule_cards, ui
from bot.handlers.auth import require_user
from bot.handlers.courses import course_title, require_course
from bot.services import repo, rules
from bot.services.llm_router import AllProvidersBusy

PAGE = 8
GROUPS = {"course": "Course", "lab": "Labs", "tutorial": "Tutorials", "assignment": "Assignments",
          "item": "Items", "disabled": "Disabled"}
ADD_SCOPES = {"course": "Whole course", "lab": "All labs", "tutorial": "All tutorials", "assignment": "All assignments"}


# ---- hub ----------------------------------------------------------------
async def show_hub(update: Update, ctx, user, edit: bool = False):
    course = await require_course(update, ctx, user)
    if not course:
        return
    async with ui.sf(ctx)() as s:
        c = await rules.rule_counts(s, course.id)
    text = (f"Rules for {course_title(course)}\n"
            f"Course {c['course']} | Labs {c['lab']} | Tutorials {c['tutorial']} | Assignments {c['assignment']} | "
            f"Items {c['item']} | Disabled {c['disabled']}\n"
            "Precedence when rules overlap: item > type (all labs...) > course.")
    rows = [[(f"Course ({c['course']})", "r:g:course:0"), (f"Labs ({c['lab']})", "r:g:lab:0"), (f"Tutorials ({c['tutorial']})", "r:g:tutorial:0")],
            [(f"Assignments ({c['assignment']})", "r:g:assignment:0"), (f"Items ({c['item']})", "r:g:item:0"),
             (f"Disabled ({c['disabled']})", "r:g:disabled:0")],
            [("Examples", "r:ex"), ("Add rule", "r:add")]]
    if c["pending"]:
        rows.insert(0, [(f"Pending decisions ({c['pending']})", "r:pend")])
    if edit and update.callback_query:
        await ui.edit_cb(update, text, ui.kb(*rows))
    else:
        await ui.reply(update, text, ui.kb(*rows))


@require_user
async def cmd_rules(update, ctx, user):
    await ui.answer(update)
    await show_hub(update, ctx, user)


@require_user
async def cb_hub(update, ctx, user):
    await ui.answer(update)
    await show_hub(update, ctx, user, edit=True)


# ---- group pages --------------------------------------------------------
async def group_rules(s, course_id: int, group: str) -> list[FeedbackRule]:
    q = select(FeedbackRule).where(FeedbackRule.course_id == course_id).order_by(FeedbackRule.id)
    if group == "disabled":
        q = q.where(FeedbackRule.status == "disabled")
    else:
        q = q.where(FeedbackRule.status.in_(("active", "pending")))
        q = q.where(FeedbackRule.scope == "course") if group == "course" else (
            q.where(FeedbackRule.scope == "item") if group == "item" else q.where(FeedbackRule.scope == "type", FeedbackRule.item_type == group))
    return list((await s.execute(q)).scalars())


def _suffix(r: FeedbackRule, item_names: dict[int, str]) -> str:
    bits = []
    if r.scope == "item" and r.item_id:
        bits.append(item_names.get(r.item_id, f"item {r.item_id}"))
    if r.origin == "worksheet":
        bits.append("from worksheets")
    if r.status == "pending":
        bits.append("pending: " + (f"conflict with #{r.conflicts_with}" if r.pending_reason == "conflict" else "spec review"))
    if r.status == "disabled" and r.disabled_reason:
        bits.append(r.disabled_reason)
    return f" ({'; '.join(bits)})" if bits else ""


async def show_group(update, ctx, user, group: str, page: int):
    course = await require_course(update, ctx, user)
    if not course:
        return
    async with ui.sf(ctx)() as s:
        allr = await group_rules(s, course.id, group)
        names = {i.id: f"{i.type.title()} {i.seq}" for i in await repo.load_plan(s, course.id)}
    chunk = allr[page * PAGE:(page + 1) * PAGE]
    lines = [f"{GROUPS[group]} rules ({len(allr)})" + (f" - page {page + 1}/{-(-len(allr) // PAGE)}" if len(allr) > PAGE else "")]
    lines += [f"#{r.id} {r.rule_text}{_suffix(r, names)}" for r in chunk] or ["(none)"]
    rows = []
    for r in chunk:
        if group == "disabled":
            rows.append([(f"Restore #{r.id}", f"r:u:{r.id}:{group}:{page}"), (f"Delete forever #{r.id}", f"r:x:{r.id}")])
        else:
            rows.append([(f"Edit #{r.id}", f"r:e:{r.id}:{group}:{page}"), (f"Disable #{r.id}", f"r:d:{r.id}:{group}:{page}"),
                         (f"Scope #{r.id}", f"r:s:{r.id}:{group}:{page}")])
    nav = ([("< Prev", f"r:g:{group}:{page - 1}")] if page > 0 else []) + ([("Next >", f"r:g:{group}:{page + 1}")] if (page + 1) * PAGE < len(allr) else [])
    rows += [nav, [("Back", "r:hub")]]
    if update.callback_query:
        await ui.edit_cb(update, "\n".join(lines), ui.kb(*rows))
    else:
        await ui.reply(update, "\n".join(lines), ui.kb(*rows))


@require_user
async def cb_group(update, ctx, user):
    await ui.answer(update)
    _, _, group, page = update.callback_query.data.split(":")
    await show_group(update, ctx, user, group, int(page))


# ---- per-rule actions ---------------------------------------------------
@require_user
async def cb_rule_op(update, ctx, user):
    parts = update.callback_query.data.split(":")
    op, rid = parts[1], int(parts[2])
    group, page = (parts[3], int(parts[4])) if len(parts) > 3 else ("course", 0)
    async with ui.sf(ctx)() as s:
        r = await s.get(FeedbackRule, rid)
        if r is None:
            await ui.answer(update, "Rule not found.")
            return
        if op == "e":
            await repo.set_state(s, user.id, mode="rule_edit", data={"rule_id": rid, "group": group, "page": page})
            await ui.answer(update)
            await ui.reply(update, f"Send the new text for rule #{rid}:\n{r.rule_text}")
            return
        if op == "d":
            batch = await rules.disable_rule(s, rid)
            await ui.answer(update, f"#{rid} disabled. Restore it from the Disabled page.")
            await show_group(update, ctx, user, group, page)
            await fbh.after_resolution(ctx.application, batch)
            return
        if op == "u":
            try:
                out = await rules.restore_rule(s, ui.llm(ctx), rid)
            except AllProvidersBusy as e:
                await ui.answer(update, f"AI providers are rate-limited; try again in ~{int(e.retry_after)} s.")
                return
            await ui.answer(update, f"#{rid} restored." if not out.pending else f"#{rid} conflicts with an active rule; decide below.")
            if out.pending:
                text, markup = await rule_cards.render_pending_page(s, r.course_id)
                await ui.reply(update, text, markup)
            await show_group(update, ctx, user, group, page)
            await fbh.after_resolution(ctx.application, out.resolved_batch)
            return
        if op == "x":
            await ui.answer(update)
            await ui.edit_cb(update, f"Delete rule #{rid} permanently? This cannot be undone.\n\"{r.rule_text}\"",
                             ui.kb([("Yes, delete forever", f"r:xx:{rid}"), ("Cancel", "r:g:disabled:0")]))
            return
        if op == "xx":
            try:
                batch = await rules.delete_rule_permanently(s, rid)
            except ValueError as e:
                await ui.answer(update, str(e)[:190])
                return
            await ui.answer(update, f"#{rid} deleted.")
            await show_group(update, ctx, user, "disabled", 0)
            await fbh.after_resolution(ctx.application, batch)
            return
        if op == "s":
            opts = []
            if r.scope != "course":
                opts.append(("Whole course", f"sh:{rid}:course"))
            for t in rules.ITEM_TYPES:
                if not (r.scope == "type" and r.item_type == t):
                    opts.append((f"All {rule_cards.TYPE_PLURAL[t]}", f"sh:{rid}:{t}"))
            fm = await s.get(FeedbackMessage, r.source_message_id) if r.source_message_id else None
            if r.scope != "item" and fm and fm.item_id:
                opts.append(("This item only", f"sh:{rid}:item"))
            await ui.answer(update)
            await ui.edit_cb(update, f"Change where rule #{rid} applies (now: {rule_cards.tag(r)}):\n\"{r.rule_text}\"",
                             ui.kb(*[[o] for o in opts], [("Back", f"r:g:{group}:{page}")]))


async def on_rule_edit_text(update, ctx, user, text: str):
    async with ui.sf(ctx)() as s:
        st = await repo.get_state(s, user.id)
        data = dict(st.data or {})
    rid = data.get("rule_id")
    try:
        async with ui.sf(ctx)() as s:
            out = await rules.edit_rule_text(s, ui.llm(ctx), rid, text)
            pend = await rule_cards.render_pending_page(s, (await s.get(FeedbackRule, rid)).course_id) if out.pending else None
    except AllProvidersBusy as e:
        await ui.reply(update, f"AI providers are rate-limited; send the new text again in ~{int(e.retry_after)} s.")
        return
    except (KeyError, ValueError):
        await ui.reply(update, "I couldn't apply that edit. Send the new rule text again.")
        return
    async with ui.sf(ctx)() as s:
        await repo.set_state(s, user.id, mode=None, data={})
    if data.get("batch"):  # editing a proposed worksheet-format rule: go back to the review message
        from bot.handlers import worksheets
        await ui.react(ctx.bot, update.effective_chat.id, update.effective_message.message_id)
        await worksheets.refresh_review(ctx.application, data["batch"], data.get("chat_id") or update.effective_chat.id, data.get("msg_id"))
        return
    if pend:
        await ui.reply(update, f"Rule #{rid} was edited, but it now conflicts with another rule:\n\n" + pend[0], pend[1])
    else:
        await ui.reply(update, f"Rule #{rid} updated. It applies to future generations.", ui.kb([("Back to rules", "r:hub")]))
    await fbh.after_resolution(ctx.application, out.resolved_batch)


# ---- add rule -----------------------------------------------------------
ADD_TEXT = "Send the rule(s). Each message can contain several requirements. Tap Done when finished."
ADD_BUTTONS = [("Done", "r:ad"), ("Cancel", "r:ax")]


@require_user
async def cb_add(update, ctx, user):
    await ui.answer(update)
    if not await require_course(update, ctx, user):
        return
    rows = [[(label, f"r:as:{k}")] for k, label in ADD_SCOPES.items()] + [[("Cancel", "r:ax")]]
    await ui.edit_cb(update, "Where should the new rule apply?", ui.kb(*rows))


@require_user
async def cb_add_scope(update, ctx, user):
    await ui.answer(update)
    choice = update.callback_query.data.split(":")[2]
    scope, itype = ("course", None) if choice == "course" else ("type", choice)
    msg_id = update.callback_query.message.message_id
    async with ui.sf(ctx)() as s:
        course = await require_course(update, ctx, user)
        await repo.set_state(s, user.id, mode="rule_add", course_id=course.id if course else None,
                             data={"scope": scope, "item_type": itype, "texts": [], "chat_id": update.effective_chat.id, "msg_id": msg_id})
    await ui.edit_cb(update, f"Adding a rule for: {ADD_SCOPES[choice]}.\n{ADD_TEXT}", ui.kb(ADD_BUTTONS))


async def on_rule_add_text(update, ctx, user, text: str):
    async with ui.sf(ctx)() as s:
        st = await repo.get_state(s, user.id)
        data = dict(st.data or {})
        data["texts"] = [*data.get("texts", []), text]
        await repo.set_state(s, user.id, data=data)
    chat_id = update.effective_chat.id
    await ui.react(ctx.bot, chat_id, update.effective_message.message_id)
    n = len(data["texts"])
    label = ADD_SCOPES["course" if data["scope"] == "course" else data["item_type"]]
    if data.get("msg_id"):
        await ui.live(ctx.bot_data, ctx.bot, chat_id, data["msg_id"]).update(
            f"Adding a rule for: {label}.\n{ADD_TEXT}\n({n} message{'s' if n != 1 else ''} received)", ui.kb(ADD_BUTTONS))


@require_user
async def cb_add_cancel(update, ctx, user):
    await ui.answer(update)
    async with ui.sf(ctx)() as s:
        st = await repo.get_state(s, user.id)
        if st.mode == "rule_add":
            await repo.set_state(s, user.id, mode=None, data={})
    await ui.edit_cb(update, "Cancelled. No rule was added.")


@require_user
async def cb_add_done(update, ctx, user):
    await ui.answer(update)
    await finish_add(ctx.application, user.id, update.effective_chat.id)


async def finish_add(app, user_id: int, chat_id: int):
    """Atomize the typed rules (scope fixed by the user's choice), check them, show the card (no regeneration)."""
    sf, llm, bot = app.bot_data["db"], app.bot_data["llm"], app.bot
    async with sf() as s:
        st = await repo.get_state(s, user_id)
        if st.mode != "rule_add":
            return
        data, course_id = dict(st.data or {}), st.course_id
        await repo.set_state(s, user_id, mode=None, data={})
    texts, msg_id, scope, itype = data.get("texts", []), data.get("msg_id"), data["scope"], data.get("item_type")
    if not texts:
        await ui.edit_or_send(bot, chat_id, msg_id, "No rule received; nothing added.")
        return
    await ui.edit_or_send(bot, chat_id, msg_id, "Reading your rules...")

    async def keep(text):
        async with sf() as s:
            await repo.set_state(s, user_id, mode="rule_add", course_id=course_id, data=data)
        await ui.edit_or_send(bot, chat_id, msg_id, text, ui.kb(ADD_BUTTONS))

    try:
        async with sf() as s:
            raw = "\n".join(t.strip() for t in texts if t.strip())
            new = await rules.extract_rules(llm, raw, None, itype, fixed_scope=scope)
            for n in new:
                n.item_type = itype if scope == "type" else None
            if not new:
                await ui.edit_or_send(bot, chat_id, msg_id, "I found no actionable rule in that text; nothing added.")
                return
            batch = await rules.create_batch(s, course_id=course_id, user_id=user_id, kind="manual", chat_id=chat_id)
            batch.card_message_id = msg_id
            out = await rules.add_rules(s, llm, batch, new, origin="manual")
            text, markup = await rule_cards.render_batch_card(s, batch, rule_cards.outcome_notes(out))
    except AllProvidersBusy as e:
        await keep(f"AI providers are rate-limited. Your text is kept - tap Done again in ~{int(e.retry_after)} s.")
        return
    except ValueError:
        await keep("I couldn't interpret that. Send a rephrased rule (or tap Done to retry).")
        return
    used = await ui.edit_or_send(bot, chat_id, msg_id, text, markup)
    if used != msg_id:
        async with sf() as s:
            (await s.get(RuleBatch, batch.id)).card_message_id = used
            await s.commit()


# ---- pending decisions + gate -------------------------------------------
@require_user
async def cb_pending(update, ctx, user):
    await ui.answer(update)
    course = await require_course(update, ctx, user)
    if not course:
        return
    async with ui.sf(ctx)() as s:
        text, markup = await rule_cards.render_pending_page(s, course.id)
    await ui.reply(update, text, markup)


async def send_gate(update, ctx, user, course):
    """Sent after the plan is confirmed: set rules before generating (optional)."""
    async with ui.sf(ctx)() as s:
        summary = await rules.summary_line(s, course.id)
        fmt = await rules.worksheet_formats(s, course.id)
        n_pending = await rules.count_pending(s, course.id)
    yn = lambda t: "yes" if fmt[t] else "no"
    text = (f"Plan confirmed. Set rules before generating (optional).\n{summary}\n"
            f"Worksheet formats: labs {yn('lab')} | tutorials {yn('tutorial')} | assignments {yn('assignment')}")
    go = ("Continue to generate", "m:gen") if not n_pending else (f"Resolve pending ({n_pending})", "r:pend")
    await ui.reply(update, text, ui.kb([("Add a rule", "r:add"), ("Build format from worksheets", "ws:menu")],
                                       [("Review rules", "m:rules"), go]))
