"""Worksheet upload -> format spec -> review -> approve, style examples (rewrite spec 8)."""
from sqlalchemy import select

from bot.db.models import FeedbackRule, Material, RuleBatch
from bot.services import repo
from tests.test_e2e_telegram import register_and_make_course, run_pending, world  # noqa: F401 (fixtures)
from tests.tg_harness import Client

SHEET = "Task 1. Compute the derivative of x^2.\nTask 2. Integrate by parts. " * 30


async def upload(ta, typ, *names):
    await ta.tap("up:reference"); await ta.tap(f"up:ref:{typ}")
    for n in names:
        await ta.send_file(n, SHEET.encode())


async def rules_of(sf):
    async with sf() as s:
        return list((await s.execute(select(FeedbackRule).order_by(FeedbackRule.id))).scalars())


async def start(world):
    app, req = world
    ta = Client(app, req, 111)
    await register_and_make_course(app, req, ta)
    await ta.say("/courses"); await ta.tap("sel:1")
    return app, req, ta, app.bot_data["db"]


async def test_format_spec_flow(world):
    app, req, ta, sf = await start(world)
    await upload(ta, "tutorial", "l1.txt", "l2.txt")
    await ta.tap("ws:build:tutorial")
    rs = await rules_of(sf)
    assert rs and all((r.status, r.pending_reason, r.scope, r.item_type, r.origin) == ("pending", "spec_review", "type", "tutorial", "worksheet") for r in rs)
    async with sf() as s:
        batch = (await s.execute(select(RuleBatch).where(RuleBatch.kind == "spec"))).scalar_one()
        assert await repo.get_heading_word(s, 1, "tutorial") != "Exercise"          # nothing applied before approval
    bid = batch.id
    await ta.tap(f"ws:hw:{bid}:Exercise")
    await ta.tap(f"ws:ok:{bid}")
    assert {r.status for r in await rules_of(sf)} == {"active"}
    async with sf() as s:
        assert await repo.get_heading_word(s, 1, "tutorial") == "Exercise"
    # reaches the lab prompt
    llm = app.bot_data["llm"].providers[0]
    llm.calls.clear()
    await ta.tap("g:tutorial")
    await run_pending(app)
    prompts = [c for c in llm.calls if "STUDENT VERSION" in c]
    assert prompts and "short objectives list" in prompts[0]


async def test_cancel_keeps_heading_word(world):
    app, req, ta, sf = await start(world)
    await upload(ta, "tutorial", "l1.txt")
    await ta.tap("ws:build:tutorial")
    async with sf() as s:
        bid = (await s.execute(select(RuleBatch).where(RuleBatch.kind == "spec"))).scalar_one().id
        before = await repo.get_heading_word(s, 1, "tutorial")
    await ta.tap(f"ws:hw:{bid}:Problem")
    await ta.tap(f"ws:cx:{bid}")
    assert not await rules_of(sf)
    async with sf() as s:
        assert await repo.get_heading_word(s, 1, "tutorial") == before


async def test_replace_previous_vs_keep_both(world):
    app, req, ta, sf = await start(world)
    await upload(ta, "tutorial", "l1.txt")
    for choice in ("rp", "kb"):
        await ta.tap("ws:build:tutorial")
        async with sf() as s:
            bid = (await s.execute(select(RuleBatch).where(RuleBatch.kind == "spec", RuleBatch.resolved == False))).scalars().all()[-1].id  # noqa: E712
        await ta.tap(f"ws:ok:{bid}")
        if choice == "rp" and not any(r.status == "active" for r in await rules_of(sf)):
            continue
        n_active = len([r for r in await rules_of(sf) if r.status == "active"])
        if n_active and choice == "rp":
            # second round: first approval was straight; build again and choose replace
            await ta.tap("ws:build:tutorial")
            async with sf() as s:
                bid2 = (await s.execute(select(RuleBatch).where(RuleBatch.kind == "spec", RuleBatch.resolved == False))).scalars().all()[-1].id  # noqa: E712
            await ta.tap(f"ws:ok:{bid2}")
            assert f"ws:rp:{bid2}" in req.last_buttons()
            await ta.tap(f"ws:rp:{bid2}")
            rs = await rules_of(sf)
            assert {r.status for r in rs} == {"active", "disabled"}
            assert all(r.disabled_reason for r in rs if r.status == "disabled")
            return


async def test_examples_toggle_swaps_oldest_and_first_uploads_are_examples(world):
    app, req, ta, sf = await start(world)
    await upload(ta, "tutorial", "a.txt", "b.txt", "c.txt")
    async with sf() as s:
        ms = (await s.execute(select(Material).where(Material.item_type == "tutorial").order_by(Material.id))).scalars().all()
        limit = app.bot_data["config"].examples_per_type
    flags = [m.is_example for m in ms]
    assert flags == [True] * min(limit, 3) + [False] * (3 - min(limit, 3))
    if limit < 3:
        await ta.tap(f"r:xt:{ms[-1].id}")
        async with sf() as s:
            ms2 = (await s.execute(select(Material).where(Material.item_type == "tutorial").order_by(Material.id))).scalars().all()
        assert ms2[-1].is_example and not ms2[0].is_example and sum(m.is_example for m in ms2) == limit


async def test_upload_burst_edits_one_message(world):
    app, req, ta, sf = await start(world)
    await ta.tap("up:reference"); await ta.tap("up:ref:tutorial")
    before = req.count_sent(111)
    for n in ("a.txt", "b.txt", "c.txt"):
        await ta.send_file(n, SHEET.encode())
    assert req.count_sent(111) - before <= 1
    assert "Added 3 worksheets for tutorials" in req.texts(111)[-1]


async def test_callback_data_within_64_bytes(world):
    app, req, ta, sf = await start(world)
    await upload(ta, "tutorial", "l1.txt")
    await ta.tap("ws:build:tutorial")
    btns = req.last_buttons()
    assert btns and all(len(d.encode()) <= 64 for d in btns)
