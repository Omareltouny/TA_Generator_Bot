"""App bootstrap. Switch polling/webhook via UPDATE_MODE."""
from __future__ import annotations

import asyncio
import logging

from telegram import BotCommand, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters)

from bot.config import Config
from bot.db.session import make_engine, make_session_factory
from bot.handlers import auth as h_auth, courses, feedback, generate, plan, review, rules_hub, ui, upload, worksheets
from bot.services import repo
from bot.services.auth import RateLimiter, get_active_user
from bot.services.jobs import JobRunner
from bot.services.llm_router import build_router

log = logging.getLogger("bot")

COMMANDS = [("start", "Register / main menu"), ("newcourse", "New course from an outline PDF"), ("courses", "Pick a course"),
            ("plan", "View/edit the term plan"), ("generate", "Generate labs/tutorials/assignments"),
            ("items", "Browse items, approve, feedback"), ("rules", "Course feedback rules"),
            ("history", "Item version history"), ("logo", "Upload logo"), ("template", "Output template"),
            ("cancel", "Cancel running job"), ("help", "Help")]


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await h_auth.token_text(update, ctx):
        return
    tg = update.effective_user
    async with ui.sf(ctx)() as s:
        user = await get_active_user(s, tg.id)
        if not user:
            await update.message.reply_text("You're not registered. Send /start and the invite token.")
            return
        st = await repo.get_state(s, user.id)
        await s.commit()
        mode = st.mode
    text = update.message.text
    if mode == "outline_review":
        return await _outline_correction(update, ctx, user, text)
    if mode in ("plan_edit", "plan_review"):
        return await plan.on_plan_edit_text(update, ctx, user, text)
    if mode == "feedback":
        return await feedback.on_feedback_text(update, ctx, user, text)
    if mode == "rule_edit":
        return await rules_hub.on_rule_edit_text(update, ctx, user, text)
    if mode == "rule_add":
        return await rules_hub.on_rule_add_text(update, ctx, user, text)
    if mode == "rule_conflict_edit":
        return await feedback.on_conflict_edit_text(update, ctx, user, text)
    if mode == "pick":
        return await generate.on_pick_text(update, ctx, user, text)
    if mode == "awaiting_outline":
        return await update.message.reply_text("Please send the outline as a PDF file.")
    reply_to = update.message.reply_to_message
    # delivered items are documents: their tag lives in the caption, not in text
    m = ui.ITEM_TAG.search(reply_to.text or reply_to.caption or "") if reply_to else None
    if m:  # replying to a delivered item = feedback on it
        return await feedback.start_feedback(update, ctx, user, int(m.group(1)), first_text=text)
    await update.message.reply_text("Use /help to see what I can do.")


async def _outline_correction(update, ctx, user, text):
    from bot.db.models import Course
    from bot.services import outline_parser as op
    from bot.services.llm_router import AllProvidersBusy
    async with ui.sf(ctx)() as s:
        st = await repo.get_state(s, user.id)
        course = await s.get(Course, st.course_id)
    try:
        new = await op.apply_corrections(ui.llm(ctx), op.Outline.model_validate(course.outline_json), text)
    except AllProvidersBusy as e:
        return await update.message.reply_text(f"Providers are rate-limited; resend in ~{int(e.retry_after)} s.")
    except ValueError:
        return await update.message.reply_text("I couldn't apply that correction. Try rephrasing.")
    async with ui.sf(ctx)() as s:
        c = await s.get(Course, course.id)
        await repo.apply_outline(c, new.model_dump())
        await s.commit()
    await ui.reply(update, op.summarize(new), ui.kb([("Continue to plan", "oc")], [("Add slides", "up:slides"), ("Add past worksheets", "up:reference")], [("Add logo", "up:logo")]))


async def cmd_help(update, ctx):
    await update.effective_message.reply_text(ui.HELP)


@h_auth.require_user
async def cb_menu(update, ctx, user):
    await ui.answer(update)
    action = update.callback_query.data.split(":")[1]
    if action == "items":
        await review.show_items(update, ctx, user)
    elif action == "plan":
        await plan.cmd_plan.__wrapped__(update, ctx, user)
    elif action == "gen":
        await generate.show_menu(update, ctx, user)
    elif action == "rules":
        await rules_hub.show_hub(update, ctx, user)
    elif action == "up":
        await update.effective_message.reply_text("What do you want to add?", reply_markup=ui.kb(
            [("Lecture slides", "up:slides"), ("Past worksheets", "up:reference")], [("Logo", "up:logo"), ("Template (.docx)", "up:template")]))
    elif action == "tpl":
        await upload.cmd_template.__wrapped__(update, ctx, user)


async def post_init(app: Application):
    sf, cfg = app.bot_data["db"], app.bot_data["config"]
    async with sf() as s:
        n = await repo.requeue_running_jobs(s)
    if n:
        log.info("requeued %d interrupted job(s)", n)

    async def notify(chat_id, text, **kw):
        await app.bot.send_message(chat_id, text, **kw)

    async def status(chat_id, message_id, text, markup=None, force=False):
        await ui.live(app.bot_data, app.bot, chat_id, message_id).update(text, markup, force=force)

    runner = JobRunner(sf, app.bot_data["llm"], deliver=review.make_deliver(app), notify=notify, status=status, cfg=cfg)
    runner.tick_hooks.append(lambda: feedback.sweep_idle(app))
    app.bot_data["runner"] = runner
    app.bot_data["worker_task"] = asyncio.create_task(runner.run_forever())
    if cfg.health_port:  # free hosts (Render/Koyeb) health-check an HTTP port even in polling mode
        async def _h(r, w):
            await r.read(1024)
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await w.drain()
            w.close()
        app.bot_data["health"] = await asyncio.start_server(_h, "0.0.0.0", cfg.health_port)
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])


async def post_shutdown(app: Application):
    runner = app.bot_data.get("runner")
    if runner:
        runner.stop()
    t = app.bot_data.get("worker_task")
    if t:
        t.cancel()


def build_app(cfg: Config, request=None) -> Application:
    b = Application.builder().token(cfg.bot_token).post_init(post_init).post_shutdown(post_shutdown)
    if request is not None:  # tests inject a fake Bot API transport
        b = b.request(request).get_updates_request(request)
    app = b.build()
    engine = make_engine(cfg.database_url)
    app.bot_data.update(config=cfg, db=make_session_factory(engine), limiter=RateLimiter(), llm=build_router(cfg))
    cmds = {"start": h_auth.start, "revoke": h_auth.revoke, "newcourse": courses.cmd_newcourse, "courses": courses.cmd_courses,
            "switch": courses.cmd_courses, "plan": plan.cmd_plan, "generate": generate.cmd_generate, "items": review.cmd_items,
            "rules": rules_hub.cmd_rules, "history": review.cmd_history, "logo": upload.cmd_logo, "template": upload.cmd_template,
            "cancel": generate.cmd_cancel, "help": cmd_help}
    for name, fn in cmds.items():
        app.add_handler(CommandHandler(name, fn))
    cb = lambda fn, pat: app.add_handler(CallbackQueryHandler(fn, pattern=pat))
    cb(courses.cb_select, r"^sel:\d+$")
    cb(courses.cmd_newcourse, r"^nc$")
    cb(cb_menu, r"^m:\w+$")
    cb(upload.cb_upload_kind, r"^up:\w+$")
    cb(upload.cb_upload_ref, r"^up:ref:\w+$")
    cb(upload.cb_template, r"^tp:\d+$")
    cb(plan.cb_outline_continue, r"^oc$")
    cb(plan.cb_confirm, r"^pc$")
    cb(plan.cb_edit, r"^pe$")
    cb(generate.cb_generate, r"^g:\w+$")
    cb(generate.cb_pick, r"^(gt:\d+|gp:\d+|gg|gx)$")
    cb(generate.cb_retry_failed, r"^rf:\d+$")
    cb(review.cb_approve, r"^ap:\d+$")
    cb(review.cb_regenerate, r"^rg:\d+$")
    cb(review.cb_tex, r"^tx:\d+$")
    cb(review.cb_items_list, r"^il:")
    cb(review.cb_item_detail, r"^it:\d+$")
    cb(review.cb_history, r"^hi:\d+$")
    cb(review.cb_history_version, r"^hv:\d+:\d+$")
    cb(review.cb_history_send, r"^hs:\d+:\d+$")
    cb(review.cb_history_rollback, r"^hr:\d+:\d+$")
    cb(feedback.cb_feedback_start, r"^fb:\d+$")
    cb(feedback.cb_feedback_done, r"^fd$")
    cb(feedback.cb_feedback_cancel, r"^fx$")
    cb(feedback.cb_conflict, r"^cf:\d+:(old|new|both|edit|back)$")
    cb(feedback.cb_scope, r"^s[ch]:\d+:\w+$")
    cb(feedback.cb_undo, r"^un:\d+$")
    cb(rules_hub.cb_hub, r"^r:hub$")
    cb(rules_hub.cb_group, r"^r:g:\w+:\d+$")
    cb(rules_hub.cb_rule_op, r"^r:(e|d|u|x|xx|s):\d+(:\w+:\d+)?$")
    cb(rules_hub.cb_add, r"^r:add$")
    cb(rules_hub.cb_add_scope, r"^r:as:\w+$")
    cb(rules_hub.cb_add_done, r"^r:ad$")
    cb(rules_hub.cb_add_cancel, r"^r:ax$")
    cb(rules_hub.cb_pending, r"^r:pend$")
    cb(worksheets.cb_examples, r"^r:ex$")
    cb(worksheets.cb_example_toggle, r"^r:xt:\d+$")
    cb(worksheets.cb_menu, r"^ws:menu$")
    cb(worksheets.cb_build, r"^ws:build:\w+$")
    cb(worksheets.cb_review, r"^ws:rev:[\w-]+$")
    cb(worksheets.cb_approve, r"^ws:ok:[\w-]+$")
    cb(worksheets.cb_approve_choice, r"^ws:(rp|kb):[\w-]+$")
    cb(worksheets.cb_cancel, r"^ws:cx:[\w-]+$")
    cb(worksheets.cb_pick_rules, r"^ws:(ed|dl):[\w-]+$")
    cb(worksheets.cb_back, r"^ws:bk:[\w-]+$")
    cb(worksheets.cb_heading, r"^ws:hw:[\w-]+:\w+$")
    cb(worksheets.cb_rule_edit, r"^ws:e:\d+$")
    cb(worksheets.cb_rule_delete, r"^ws:d:\d+$")
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, upload.on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs URLs containing the bot token
    cfg = Config.from_env()
    missing = [k for k, v in {"TELEGRAM_BOT_TOKEN": cfg.bot_token, "INVITE_TOKEN": cfg.invite_token, "DATABASE_URL": cfg.database_url}.items() if not v]
    if missing:
        raise SystemExit(f"Missing required env vars: {', '.join(missing)}")
    if not any(cfg.api_keys.get(n) for n in cfg.provider_order):
        raise SystemExit("Set at least one of GROQ_API_KEY / GEMINI_API_KEY / OPENROUTER_API_KEY")
    app = build_app(cfg)
    if cfg.update_mode == "webhook":
        app.run_webhook(listen="0.0.0.0", port=cfg.port, webhook_url=cfg.webhook_url)
    else:
        app.run_polling()


if __name__ == "__main__":
    main()
