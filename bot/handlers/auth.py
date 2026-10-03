"""/start token gate and /revoke. Also exposes `require_user` for every other handler."""
from __future__ import annotations

import functools

from telegram import Update
from telegram.ext import ContextTypes

from bot.services import auth


def require_user(fn):
    """Decorator: only registered, non-revoked users reach the handler. Passes `user` kwarg."""
    @functools.wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        tg = update.effective_user
        async with ctx.bot_data["db"]() as s:
            user = await auth.get_active_user(s, tg.id) if tg else None
        if not user:
            if update.effective_message:
                await update.effective_message.reply_text("You're not registered. Send /start and the invite token.")
            return
        return await fn(update, ctx, user=user)
    return wrapper


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    async with ctx.bot_data["db"]() as s:
        if await auth.get_active_user(s, tg.id):
            await update.message.reply_text("Welcome back. Use /courses or /newcourse.")
            return
    ctx.user_data["awaiting_token"] = True
    await update.message.reply_text("Please paste the invite token to register.")


async def token_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle a message while awaiting a token. Returns True if consumed."""
    if not ctx.user_data.get("awaiting_token"):
        return False
    tg, cfg, limiter = update.effective_user, ctx.bot_data["config"], ctx.bot_data["limiter"]
    if limiter.blocked(tg.id):
        await update.message.reply_text("Too many attempts. Try again later.")
        return True
    if auth.token_matches(update.message.text or "", cfg.invite_token):
        async with ctx.bot_data["db"]() as s:
            await auth.register(s, tg.id, tg.full_name)
        ctx.user_data.pop("awaiting_token", None)
        await update.message.reply_text("Registered. Use /newcourse to start or /courses to browse.")
    else:
        limiter.record(tg.id)
        await update.message.reply_text("Invalid token.")
    return True


async def revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg = ctx.bot_data["config"]
    if cfg.owner_telegram_id is None or update.effective_user.id != cfg.owner_telegram_id:
        return  # silently ignore non-owners
    if not ctx.args:
        await update.message.reply_text("Usage: /revoke <telegram_id|name>")
        return
    async with ctx.bot_data["db"]() as s:
        u = await auth.revoke(s, ctx.args[0])
    await update.message.reply_text(f"Revoked {u.name or u.telegram_id}." if u else "No such user.")
