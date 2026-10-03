"""Shared Telegram helpers: keyboards, long-message splitting, item labels, state access."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

STATUS = {"planned": "planned", "generating": "generating", "draft": "draft", "approved": "approved", "failed": "FAILED"}
ITEM_TAG = re.compile(r"\[item #(\d+)\]")


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows if row])


def sf(ctx):
    return ctx.bot_data["db"]


def cfg(ctx):
    return ctx.bot_data["config"]


def llm(ctx):
    return ctx.bot_data["llm"]


def label(item, with_status=True) -> str:
    base = f"{item.type[:3].title()} {item.seq} - W{item.week or '?'} - {item.title[:34]}"
    return f"{base} [{STATUS.get(item.status, item.status)}]" if with_status else base


def split_text(text: str, limit: int = 3900) -> list[str]:
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out + [text] if text else out


async def reply(update: Update, text: str, markup=None):
    """Reply in the chat; long text is split (only the last chunk carries the keyboard)."""
    chunks = split_text(text) or ["(empty)"]
    msg = None
    for i, c in enumerate(chunks):
        msg = await update.effective_message.reply_text(c, reply_markup=markup if i == len(chunks) - 1 else None)
    return msg


async def answer(update: Update, text: str | None = None):
    if update.callback_query:
        await update.callback_query.answer(text)


def aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


HELP = """TA Course-Material Bot

/newcourse - upload an outline (PDF) and start a course
/courses (or /switch) - pick a course
/plan - view/edit the term plan
/generate - generate labs, tutorials, assignments
/items - browse items, approve, give feedback
/rules - view/edit/delete the course feedback rules
/history <item> - versions of an item (e.g. /history lab 3)
/logo - upload a logo for document headers
/template - choose or upload a .docx output template
/cancel - cancel your running job
/help - this message

Tip: reply to any delivered item's message with text to give feedback on it."""
