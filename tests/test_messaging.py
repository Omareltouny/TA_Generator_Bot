"""Telegram noise budget and throttling (rewrite spec 10)."""
from sqlalchemy import select

from bot.db.models import PlanItem
from bot.handlers.ui import LiveMessage
from tests.test_e2e_telegram import register_and_make_course, run_pending, world  # noqa: F401 (fixtures)
from tests.tg_harness import Client


def sends(req, since):
    new = req.calls[since:]
    return ([p for n, p in new if n == "sendMessage"], [p for n, p in new if n == "sendDocument"],
            [p for n, p in new if n == "editMessageText"])


async def test_message_budget(world):
    app, req = world
    sf, ta = app.bot_data["db"], Client(app, req, 111)
    await register_and_make_course(app, req, ta)
    await ta.tap("pc")
    mark = len(req.calls)
    await ta.tap("g:assignment")                       # 3 items
    await run_pending(app)
    msgs, docs, edits = sends(req, mark)
    assert len(msgs) == 2 and len(docs) == 6           # 1 status + 1 final, 6 documents, no zip
    assert not any(p["__files__"][0][0].endswith(".zip") for p in docs)
    # only the status message is edited; all documents are muted for a multi-item job
    assert all(p.get("disable_notification") for p in docs)
    assert "Done: 3 generated." in req.texts(111)[-1]
    # caption format: answer key first, then the student sheet with tag + hint
    assert docs[0]["caption"].startswith("Answer key | Assignment 1 v1") and "[item #" in docs[0]["caption"]
    assert "rules applied" in docs[1]["caption"] and "Reply to this message to give feedback." in docs[1]["caption"]

    # single-item job: no final message, student document is NOT muted
    async with sf() as s:
        iid = (await s.execute(select(PlanItem).where(PlanItem.type == "assignment", PlanItem.seq == 1))).scalar_one().id
    mark = len(req.calls)
    await ta.tap(f"rg:{iid}")
    await run_pending(app)
    msgs, docs, edits = sends(req, mark)
    assert len(msgs) == 1 and len(docs) == 2
    assert docs[0].get("disable_notification") and not docs[1].get("disable_notification")
    assert "v2 ready" in req.texts(111)[-1]


async def test_feedback_round_budget(world):
    app, req = world
    sf, ta = app.bot_data["db"], Client(app, req, 111)
    await register_and_make_course(app, req, ta)
    await ta.tap("pc")
    await ta.tap("g:assignment"); await run_pending(app)
    async with sf() as s:
        iid = (await s.execute(select(PlanItem).where(PlanItem.type == "assignment", PlanItem.seq == 1))).scalar_one().id
    await ta.tap(f"fb:{iid}")
    await ta.say("Use shorter questions")
    mark = len(req.calls)
    await ta.tap("fd")
    await run_pending(app)
    msgs, docs, _ = sends(req, mark)
    assert len(docs) == 2 and len(msgs) <= 1           # card is an edit; at most the status message is new


class Bot:
    def __init__(self): self.edits = []
    async def edit_message_text(self, text, **kw): self.edits.append(text)


async def test_live_message_throttle():
    now = [0.0]
    bot = Bot()
    lm = LiveMessage(bot, 1, 2, min_interval=3.0, clock=lambda: now[0])
    assert await lm.update("a") is True
    assert await lm.update("b") is False and await lm.update("c") is False   # throttled, remembered
    assert bot.edits == ["a"]
    now[0] = 1.0
    assert await lm.flush() is True and bot.edits == ["a", "c"]                # latest text wins
    assert await lm.update("c") is False                                       # identical -> no edit
    now[0] = 10.0
    assert await lm.update("d") is True
    assert await lm.update("e", force=True) is True                            # force bypasses the interval
    assert bot.edits == ["a", "c", "d", "e"]
