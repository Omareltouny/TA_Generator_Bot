"""Shared Telegram helpers: keyboards, long-message splitting, item labels, state access."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReactionTypeEmoji, Update
from telegram.error import BadRequest, RetryAfter

log = logging.getLogger("ui")

STATUS = {"planned": "planned", "generating": "generating", "draft": "draft", "approved": "approved", "failed": "FAILED"}
ITEM_TAG = re.compile(r"\[item #(\d+)\]")


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows if row])


def sf(ctx):
    return ctx.bot_data["db"]


def cfg(ctx):
    return ctx.bot_data["config"]


def ocr_options(ctx):
    """OcrOptions from config, or None when OCR is disabled (OCR_ENABLED=0)."""
    from bot.services.pdf_text import OcrOptions
    c = cfg(ctx)
    return OcrOptions(c.ocr_langs, c.ocr_dpi, c.ocr_max_pages) if c.ocr_enabled else None


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


def _markup_key(markup) -> str | None:
    return markup.to_json() if markup is not None and hasattr(markup, "to_json") else None


class LiveMessage:
    """One Telegram message that is edited in place, with throttling (rewrite spec 10.1).

    * `update()` sends at most one edit per `min_interval` seconds; a throttled update is remembered and delivered by
      the next `update()` / `flush()` (callers always pass the full current text, so nothing is lost).
    * `force=True` bypasses the throttle (use for final states).
    * Swallows "message is not modified", retries once after `RetryAfter`, and never raises into the caller.
    """

    def __init__(self, bot, chat_id: int, message_id: int, *, min_interval: float = 3.0,
                 clock=time.monotonic, sleep=asyncio.sleep):
        self.bot, self.chat_id, self.message_id = bot, chat_id, message_id
        self.min_interval, self._clock, self._sleep = min_interval, clock, sleep
        self._last_at: float | None = None
        self._last_key: tuple | None = None
        self._pending: tuple | None = None
        self.edits = 0  # edits actually sent (observability / tests)

    async def update(self, text: str, reply_markup=None, force: bool = False) -> bool:
        now = self._clock()
        if not force and self._last_at is not None and now - self._last_at < self.min_interval:
            self._pending = (text, reply_markup)
            return False
        return await self._send(text, reply_markup)

    async def flush(self) -> bool:
        """Send the most recent throttled update, if any (ignores the interval)."""
        if self._pending is None:
            return False
        text, markup = self._pending
        return await self._send(text, markup)

    async def _send(self, text: str, markup) -> bool:
        self._pending = None
        key = (text, _markup_key(markup))
        if key == self._last_key:
            return False
        self._last_at = self._clock()
        for attempt in range(2):
            try:
                await self.bot.edit_message_text(text, chat_id=self.chat_id, message_id=self.message_id, reply_markup=markup)
                self._last_key = key
                self.edits += 1
                return True
            except RetryAfter as e:
                if attempt == 1:
                    return False
                await self._sleep(float(getattr(e, "retry_after", 1) or 1))
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    self._last_key = key
                else:
                    log.warning("edit failed: %s", e)
                return False
            except Exception:
                log.exception("live message edit failed")
                return False
        return False


_LIVE_CACHE_MAX = 300


def live(bot_data: dict, bot, chat_id: int, message_id: int) -> LiveMessage:
    """Cached LiveMessage per (chat, message) so throttling state survives across handler calls."""
    cache = bot_data.setdefault("live_messages", {})
    key = (chat_id, message_id)
    if key not in cache:
        if len(cache) >= _LIVE_CACHE_MAX:
            cache.pop(next(iter(cache)))
        cfg = bot_data.get("config")
        cache[key] = LiveMessage(bot, chat_id, message_id,
                                 min_interval=getattr(cfg, "status_edit_min_interval_s", 3.0))
    return cache[key]


async def edit_or_send(bot, chat_id: int, message_id: int | None, text: str, markup=None) -> int | None:
    """Edit a message in place; if that is impossible (deleted, too old) send a new one. Returns the message id used."""
    if message_id:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
            return message_id
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return message_id
            log.info("edit failed (%s); sending a new message", e)
        except Exception:
            log.exception("edit failed; sending a new message")
    msg = await bot.send_message(chat_id, text, reply_markup=markup)
    return msg.message_id


async def edit_cb(update: Update, text: str, markup=None) -> None:
    """Edit the message a callback button belongs to (falls back to a reply)."""
    q = update.callback_query
    try:
        await q.edit_message_text(text, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            await reply(update, text, markup)


async def react(bot, chat_id: int, message_id: int, emoji: str = "\U0001F44D") -> None:
    """Thumbs-up reaction on the user's message instead of a text reply. Failures are ignored."""
    try:
        await bot.set_message_reaction(chat_id, message_id, reaction=[ReactionTypeEmoji(emoji)])
    except Exception:
        pass


def aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


HELP = """TA Course-Material Bot

/newcourse - upload an outline (PDF) and start a course
/courses (or /switch) - pick a course
/plan - view/edit the term plan
/generate - generate labs, tutorials, assignments
/items - browse items, approve, give feedback
/rules - rules hub: course, per-type and per-item rules, add rule, worksheet examples
/history <item> - versions of an item (e.g. /history lab 3)
/logo - upload a logo for document headers
/template - choose or upload a .docx output template
/cancel - cancel your running job
/help - this message

Tip: reply to any delivered item's message with text to give feedback on it."""
