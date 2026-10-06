"""Handler-level tests for the rules UX (rewrite spec 6, 7): cards, conflicts, blocked generation, gate."""
import pytest
from sqlalchemy import select

from bot.db.models import FeedbackRule, Job, PlanItem
from bot.services import repo
from tests.helpers import make_llm
from tests.test_e2e_telegram import register_and_make_course, run_pending, world  # noqa: F401 (fixtures)
from tests.tg_harness import Client


async def rules_of(sf):
    async with sf() as s:
        return list((await s.execute(select(FeedbackRule).order_by(FeedbackRule.id))).scalars())


async def jobs_of(sf):
    async with sf() as s:
        return list((await s.execute(select(Job).order_by(Job.id))).scalars())


async def tutorial1(sf):
    async with sf() as s:
        return (await s.execute(select(PlanItem).where(PlanItem.type == "tutorial", PlanItem.seq == 1))).scalar_one().id


async def add_course_rule(ta, req, text):
    await ta.tap("r:add")
    await ta.tap("r:as:course")
    await ta.say(text)
    await ta.tap("r:ad")


async def test_two_feedback_rounds_keep_both_through_telegram(world):
    app, req = world
    sf, ta = app.bot_data["db"], Client(app, req, 111)
    await register_and_make_course(app, req, ta)
    t1 = await tutorial1(sf)
    await ta.tap(f"fb:{t1}"); await ta.say("Q3 answer should be 42"); await ta.tap("fd")
    await run_pending(app)
    await ta.tap(f"fb:{t1}"); await ta.say("Shorten question 2 to one sentence"); await ta.tap("fd")
    llm = app.bot_data["llm"].providers[0]
    llm.calls.clear()
    await run_pending(app)
    assert [r.status for r in await rules_of(sf)] == ["active", "active"]
    prompts = [c for c in llm.calls if "STUDENT VERSION" in c]
    assert prompts and "Q3 answer should be 42" in prompts[0] and "Shorten question 2" in prompts[0]
    async with sf() as s:
        assert (await s.get(PlanItem, t1)).current_version == 2


async def test_manual_conflict_blocks_generation_and_is_decided_by_user(world):
    app, req = world
    sf, ta = app.bot_data["db"], Client(app, req, 111)
    await register_and_make_course(app, req, ta)
    await add_course_rule(ta, req, "Use Python")
    assert [r.status for r in await rules_of(sf)] == ["active"]
    await add_course_rule(ta, req, "Use Java")
    py, java = await rules_of(sf)
    assert (java.status, java.pending_reason, java.conflicts_with) == ("pending", "conflict", py.id)
    card = 5   # the add-rule flow edits the message its buttons belong to (the harness taps message id 5)
    text = req.card_text(card)
    assert f"#{java.id} conflicts with #{py.id}. Reason: " in text and "Python" in text      # reason is shown
    assert {f"cf:{java.id}:{c}" for c in ("old", "new", "both", "edit")} <= set(req.buttons_of_message(card))
    # generation is blocked while anything is pending, and says why
    await ta.tap("g:tutorial")
    assert "waiting for your decision" in req.texts(111)[-1] and not await jobs_of(sf)
    # Keep the old rule: the new one is disabled (not deleted) and can be restored
    await ta.tap(f"cf:{java.id}:old", msg_id=card)
    py, java = await rules_of(sf)
    assert (py.status, java.status, java.disabled_reason) == ("active", "disabled", f"rejected: you kept #{py.id}")
    await ta.tap("g:tutorial")                       # unblocked now
    assert len(await jobs_of(sf)) == 1
    await ta.tap(f"r:u:{java.id}:disabled:0")        # restoring re-runs the conflict check -> pending again
    py, java = await rules_of(sf)
    assert java.status == "pending" and py.status == "active"
    await ta.tap(f"cf:{java.id}:new")                # pending page (not a batch card): Keep new
    py, java = await rules_of(sf)
    assert (py.status, java.status, py.replaced_by) == ("disabled", "active", java.id)


async def test_regen_waits_for_resolution_then_fires_exactly_once(world):
    app, req = world
    sf, ta = app.bot_data["db"], Client(app, req, 111)
    await register_and_make_course(app, req, ta)
    await add_course_rule(ta, req, "Use Python")
    t1 = await tutorial1(sf)
    await ta.tap(f"fb:{t1}"); await ta.say("Use Java for everything"); await ta.tap("fd")
    py, java = await rules_of(sf)
    assert java.status == "pending" and py.status == "active"
    assert not await jobs_of(sf), "regeneration must wait for the user's decision"
    card = req.msg_id("Feedback on", 111)
    assert "Regeneration starts after you decide." in req.card_text(card)
    await ta.tap(f"cf:{java.id}:both", msg_id=card)
    jobs = await jobs_of(sf)
    assert len(jobs) == 1 and jobs[0].payload["origin"] == "feedback" and jobs[0].payload["item_ids"] == [t1]
    assert "Regenerating Tutorial 1 with all rules" in req.card_text(card)
    await ta.tap(f"cf:{java.id}:both", msg_id=card)  # double tap: still one job
    assert len(await jobs_of(sf)) == 1
    assert [r.status for r in await rules_of(sf)] == ["active", "active"]


async def test_conflict_edit_replaces_text_and_rechecks(world):
    app, req = world
    sf, ta = app.bot_data["db"], Client(app, req, 111)
    await register_and_make_course(app, req, ta)
    await add_course_rule(ta, req, "Use Python")
    t1 = await tutorial1(sf)
    await ta.tap(f"fb:{t1}"); await ta.say("Use Java for everything"); await ta.tap("fd")
    _, java = await rules_of(sf)
    card = req.msg_id("Feedback on", 111)
    await ta.tap(f"cf:{java.id}:edit", msg_id=card)
    await ta.say("Show marks on every question")        # no longer conflicts -> activates -> regeneration queued
    py, java = await rules_of(sf)
    assert java.rule_text == "Show marks on every question" and java.status == "active" and py.status == "active"
    assert len(await jobs_of(sf)) == 1


async def test_widen_and_undo_buttons(world):
    app, req = world
    sf, ta = app.bot_data["db"], Client(app, req, 111)
    await register_and_make_course(app, req, ta)
    t1 = await tutorial1(sf)
    await ta.tap(f"fb:{t1}"); await ta.say("Q3 answer should be 42"); await ta.tap("fd")
    (r,) = await rules_of(sf)
    card = req.msg_id("Feedback on", 111)
    assert {f"sc:{r.id}:type", f"sc:{r.id}:course", f"un:{r.id}"} <= set(req.buttons_of_message(card))
    await ta.tap(f"sc:{r.id}:type", msg_id=card)
    (r,) = await rules_of(sf)
    assert (r.scope, r.item_type, r.item_id) == ("type", "tutorial", None)
    assert "[all tutorials]" in req.card_text(card)
    await ta.tap(f"un:{r.id}", msg_id=card)
    (r,) = await rules_of(sf)
    assert r.status == "disabled" and r.disabled_reason == "undone by you"


async def test_gate_after_plan_confirmation(world):
    app, req = world
    sf, ta = app.bot_data["db"], Client(app, req, 111)
    await register_and_make_course(app, req, ta)
    gate = [t for t in req.texts(111) if "Set rules before generating" in t][-1]
    assert "Rules in effect: course 0 | labs 0 | tutorials 0 | assignments 0" in gate and "Worksheet formats" in gate
    assert {"r:add", "m:rules", "m:gen"} <= set(req.last_buttons())
    await add_course_rule(ta, req, "Use Python"); await add_course_rule(ta, req, "Use Java")
    await ta.tap("pc")                                    # re-showing the gate with a pending rule
    assert "r:pend" in req.last_buttons() and "m:gen" not in req.last_buttons()
    await ta.tap("m:gen")
    await ta.tap("r:pend")
    assert "Pending decisions (1)" in req.texts(111)[-1]
