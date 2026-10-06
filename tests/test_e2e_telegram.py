"""End-to-end through the real handlers with a fake Bot API: onboarding -> outline -> plan -> generate -> review -> feedback."""
import pathlib

import pytest
from sqlalchemy import select

from bot.config import Config
from bot.db.models import Base, Course, FeedbackRule, ItemVersion, PlanItem
from bot.main import build_app, post_init
from bot.services import repo
from tests.helpers import Scripted, make_llm
from tests.tg_harness import Client, FakeRequest

FIX = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
async def world(tmp_path):
    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "123:ABC", "INVITE_TOKEN": "s3cret", "OWNER_TELEGRAM_ID": "900",
                           "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path}/e2e.db", "GROQ_API_KEY": "x"})
    req = FakeRequest()
    app = build_app(cfg, request=req)
    # Create schema, swap in the scripted LLM, then run the real post_init (starts worker + boot recovery).
    from sqlalchemy.ext.asyncio import create_async_engine
    eng = create_async_engine(cfg.database_url)
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    app.bot_data["llm"] = make_llm()
    await app.initialize()
    await post_init(app)
    app.bot_data["worker_task"].cancel()  # tests drive jobs deterministically via run_pending()
    yield app, req
    await app.shutdown()
    await eng.dispose()


async def run_pending(app):
    runner = app.bot_data["runner"]
    while True:
        async with app.bot_data["db"]() as s:
            job = await repo.claim_next_job(s)
        if not job:
            return
        await runner.run_job(job.id)


async def test_full_user_journey(world):
    app, req = world
    sf = app.bot_data["db"]
    ta, ta2, owner = Client(app, req, 111, "Ann"), Client(app, req, 222, "Bob"), Client(app, req, 900, "Owner")

    # --- access: blocked until registered; wrong token rejected; right token registers
    await ta.say("/courses")
    assert any("not registered" in t for t in req.texts(111))
    await ta.say("/start"); await ta.say("wrong")
    assert "Invalid token." in req.texts(111)
    await ta.say("/courses")
    assert sum("not registered" in t for t in req.texts(111)) == 2
    await ta.say("/start"); await ta.say("s3cret")
    assert any("Registered" in t for t in req.texts(111))

    # --- new course from a REAL outline PDF (LLM stubbed)
    await ta.say("/newcourse")
    await ta.send_file("Math1920.pdf", (FIX / "math1920.pdf").read_bytes())
    assert any("MATH 1920" in t and "Single Variable Calculus II" in t for t in req.texts(111))
    assert "oc" in req.last_buttons()

    # --- plan: 12 tutorials + 3 assignments, nothing generates before confirmation
    await ta.tap("oc")
    plan_text = "\n".join(req.texts(111))
    assert "TUTORIALS" in plan_text and "ASSIGNMENTS" in plan_text and "Assignment 3" in plan_text
    async with sf() as s:
        items = await repo.load_plan(s, 1)
        assert len(items) == 15 and all(i.status == "planned" for i in items)
    await ta.tap("g:all")
    assert any("Confirm the plan first" in t for t in req.texts(111))
    # plan edit via free text
    await ta.tap("pe"); await ta.say("drop tutorial 12")
    async with sf() as s:
        assert len(await repo.load_plan(s, 1)) == 14
    await ta.tap("pc")
    assert "Plan confirmed" in "\n".join(req.texts(111))

    # --- selective generation: all assignments -> 3 x 2 docx delivered, answer key first, then student
    await ta.tap("g:assignment")
    await run_pending(app)
    names = req.doc_names(111)
    assert names == [f"MATH_1920_assignment{i}_{k}.docx" for i in (1, 2, 3) for k in ("answer_key", "student")]
    doc_btns = [req._buttons_of(p) for n, p in req.calls if n == "sendDocument"]
    assert len([b for b in doc_btns if b]) == 3 and all(any(x.startswith("ap:") for x in b) and any(x.startswith("fb:") for x in b) for b in doc_btns if b)
    assert {"il:all:all:0"} <= set(req.last_buttons())          # one final message with [Review items]

    # --- another TA sees and can act on the same course; revoked user is blocked again
    await ta2.say("/start"); await ta2.say("s3cret")
    await ta2.say("/courses")
    assert any("Courses (shared" in t for t in req.texts(222))
    await owner.say("/revoke 222")
    await ta2.say("/courses")
    assert req.texts(222)[-1].startswith("You're not registered")
    await ta.say("/revoke 111")  # non-owner is ignored
    async with sf() as s:
        from bot.services.auth import get_active_user
        assert await get_active_user(s, 111)

    # --- review: approve, then reply-to feedback -> new version, status back to draft
    async with sf() as s:
        a1 = (await s.execute(select(PlanItem).where(PlanItem.type == "assignment", PlanItem.seq == 1))).scalar_one()
    await ta.tap(f"ap:{a1.id}")
    async with sf() as s:
        assert (await s.get(PlanItem, a1.id)).status == "approved"
    await ta.say("Use Java for everything", reply_to_caption=f"Assignment 1\nv1 | draft\n[item #{a1.id}]")
    await ta.tap("fd")
    assert any("[course] Use Java for everything" in t for t in req.texts(111))
    await run_pending(app)
    async with sf() as s:
        item = await s.get(PlanItem, a1.id)
        assert item.status == "draft" and item.current_version == 2
        assert len(await repo.list_versions(s, a1.id)) == 2
        assert (await s.execute(select(FeedbackRule).where(FeedbackRule.status == "active"))).scalars().first().rule_text == "Use Java for everything"

    # --- /rules hub shows the group, /history, rollback
    await ta.say("/rules")
    assert any("Course 1" in t for t in req.texts(111)[-2:])
    await ta.tap("r:g:course:0")
    assert any("Use Java for everything" in t for t in req.texts(111)[-2:])
    await ta.say("/history assignment 1")
    assert any("History of assignment 1" in t for t in req.texts(111))
    await ta.tap(f"hr:{a1.id}:1")
    async with sf() as s:
        assert (await s.get(PlanItem, a1.id)).current_version == 3

    # --- items browser and .tex export
    await ta.say("/items")
    assert any("Items (14)" in t for t in req.texts(111))
    before = len(req.doc_names(111))
    await ta.tap(f"tx:{a1.id}")
    assert req.doc_names(111)[before:] == ["MATH_1920_assignment1_student.tex", "MATH_1920_assignment1_answer_key.tex"]


async def register_and_make_course(app, req, ta):
    await ta.say("/start"); await ta.say("s3cret")
    await ta.say("/newcourse")
    await ta.send_file("Math1920.pdf", (FIX / "math1920.pdf").read_bytes())
    await ta.tap("oc"); await ta.tap("pc")


async def test_materials_logo_pick_retry_idle_rules(world):
    import io, zipfile, base64
    from bot.handlers import feedback as fbh
    from tests.test_renderer import tiny_png
    app, req = world
    sf, llm = app.bot_data["db"], app.bot_data["llm"].providers[0]
    ta = Client(app, req, 111)
    await register_and_make_course(app, req, ta)

    # reference material as a ZIP (+ junk that must be skipped) feeds retrieval into prompts
    await ta.say("/courses"); await ta.tap("sel:1"); await ta.tap("up:reference"); await ta.tap("up:ref:tutorial")
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("old/lab1.txt", "Integration by substitution worked example: let u = g(x). " * 40)
        zf.writestr("photo.png", b"x"); zf.writestr("../evil.txt", b"x")
    await ta.send_file("past.zip", z.getvalue())
    msg = [t for t in req.texts(111) if t.startswith("Added")][-1]
    assert "Added 1 worksheet for tutorials" in msg and "Skipped 2" in msg

    # logo
    await ta.say("/logo")
    await ta.send_file("logo.png", tiny_png())
    assert any("Logo saved" in t for t in req.texts(111))

    # pick items by free text: "tutorial 1" and "assignment 2"
    await ta.tap("g:pick")
    await ta.say("tutorial 1 and assignment 2")
    llm.calls.clear()
    await run_pending(app)
    assert len(req.doc_names(111)) == 4
    assert any("Integration by substitution" in c for c in llm.calls)          # reference excerpt retrieved
    async with sf() as s:
        t1 = (await s.execute(select(PlanItem).where(PlanItem.type == "tutorial", PlanItem.seq == 1))).scalar_one()
        v = await repo.get_version(s, t1.id)
        import zipfile as zf2
        assert any("media/" in n for n in zf2.ZipFile(io.BytesIO(v.student_docx)).namelist())   # logo embedded
        assert t1.title.startswith("Worksheet 1:")                                              # generic title enriched

    # failure + "Retry failed"
    llm.fail_titles = {"Worksheet 2"}
    await ta.tap("g:tutorial")
    await run_pending(app)
    assert any("1 failed" in t or "failed" in t for t in req.texts(111)[-3:])
    retry = [b for b in req.last_buttons() if b.startswith("rf:")]
    assert retry
    llm.fail_titles = set()
    await ta.tap(retry[0])
    await run_pending(app)
    async with sf() as s:
        st = {i.status for i in await repo.load_plan(s, 1) if i.type == "tutorial"}
    assert st == {"draft"}

    # feedback idle timeout closes input automatically (DB-backed sweep, no Done press)
    await ta.tap(f"fb:{t1.id}")
    await ta.say("Q3 answer should be 42")
    from datetime import datetime, timedelta, timezone
    from bot.db.models import UserState
    async with sf() as s:
        (await s.get(UserState, 1)).updated_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        await s.commit()
    await fbh.sweep_idle(app)
    assert any("[this item] Q3 answer should be 42" in t for t in req.texts(111))
    await run_pending(app)
    async with sf() as s:
        assert (await s.get(PlanItem, t1.id)).current_version == 2
        rule = (await s.execute(select(FeedbackRule).where(FeedbackRule.item_id == t1.id))).scalar_one()
    # widen to course scope from the card, edit it, then disable it via /rules buttons
    card = req.msg_id("Feedback on", 111)
    await ta.tap(f"sc:{rule.id}:course", msg_id=card)
    async with sf() as s:
        r = await s.get(FeedbackRule, rule.id)
        assert r.scope == "course" and r.item_id is None and r.status == "active"
    await ta.tap(f"r:e:{rule.id}:course:0"); await ta.say("Always show final answers in bold")
    async with sf() as s:
        assert (await s.get(FeedbackRule, rule.id)).rule_text == "Always show final answers in bold"
    await ta.tap(f"r:d:{rule.id}:course:0")
    async with sf() as s:
        assert (await s.get(FeedbackRule, rule.id)).status == "disabled"

    # /cancel on a queued job leaves items untouched
    await ta.tap("g:assignment")
    await ta.say("/cancel")
    await run_pending(app)
    async with sf() as s:
        a1 = (await s.execute(select(PlanItem).where(PlanItem.type == "assignment", PlanItem.seq == 1))).scalar_one()
        assert a1.status == "planned"


async def test_bad_uploads(world):
    app, req = world
    ta = Client(app, req, 111)
    await ta.say("/start"); await ta.say("s3cret"); await ta.say("/newcourse")
    await ta.send_file("notes.docx", b"x")
    assert any("must be a PDF" in t for t in req.texts(111))
    await ta.send_file("broken.pdf", b"%PDF-garbage")
    assert any("couldn't read that PDF" in t for t in req.texts(111))


async def test_plan_edit_without_pressing_edit(world):
    app, req = world
    ta = Client(app, req, 111)
    await ta.say("/start"); await ta.say("s3cret"); await ta.say("/newcourse")
    await ta.send_file("Math1920.pdf", (FIX / "math1920.pdf").read_bytes())
    await ta.tap("oc")
    await ta.say("drop tutorial 12")  # typed straight after the plan, no Edit button
    async with app.bot_data["db"]() as s:
        assert len(await repo.load_plan(s, 1)) == 14
