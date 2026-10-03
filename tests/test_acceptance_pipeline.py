"""Acceptance tests (spec 13) for generation, feedback loop, restart and failure handling with a scripted LLM."""
import io
import zipfile

import pytest
from sqlalchemy import select

from bot.db.models import FeedbackRule, ItemVersion, Job, PlanItem
from bot.services import feedback_store as fb, jobs, repo
from bot.services.jobs import JobRunner
from tests.helpers import Scripted, make_llm, seed


async def run(session_factory_like, session, llm, item_ids, user, course):
    """Create + run a job synchronously using the same session factory style the bot uses."""
    j = await repo.create_job(session, course.id, "generate", {"item_ids": item_ids, "chat_id": 1}, user.id)
    sent = []

    async def notify(chat, text, **kw): sent.append(text)
    runner = JobRunner(session_factory_like, llm, notify=notify)
    return j, runner, sent


@pytest.fixture
async def env(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine
    from bot.db.models import Base
    from bot.db.session import make_session_factory
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    sf = make_session_factory(eng)
    async with sf() as s:
        u, c, items = await seed(s)
        ids = {(i.type, i.seq): i.id for i in items}
        uid, cid = u.id, c.id
    yield sf, uid, cid, ids
    await eng.dispose()


async def gen(sf, llm, uid, cid, item_ids):
    async with sf() as s:
        j = await repo.create_job(s, cid, "generate", {"item_ids": item_ids, "chat_id": 1}, uid)
        jid = j.id
    msgs = []

    async def notify(chat, text, **kw): msgs.append(text)
    r = JobRunner(sf, llm, notify=notify)
    async with sf() as s:
        claimed = await repo.claim_next_job(s)
    assert claimed.id == jid
    return await r.run_job(jid), msgs


async def test_generation_two_docx_numbering_no_draft_text(env):
    sf, uid, cid, ids = env
    status, _ = await gen(sf, make_llm(), uid, cid, [ids[("lab", 1)]])
    assert status == "done"
    async with sf() as s:
        v = await repo.get_version(s, ids[("lab", 1)])
        assert v.student_docx[:2] == b"PK" and v.key_docx[:2] == b"PK"
        z = zipfile.ZipFile(io.BytesIO(v.student_docx)).read("word/document.xml").decode()
        assert "<m:oMath" in z
        low = (v.student_md + v.key_md).lower()
        assert "draft" not in low and "review" not in low
        item = await s.get(PlanItem, ids[("lab", 1)])
        assert item.status == "draft" and item.current_version == 1


async def test_failures_do_not_abort_batch_and_retry(env):
    sf, uid, cid, ids = env
    llm = make_llm(Scripted(fail_titles=["Lab 2"]))
    status, msgs = await gen(sf, llm, uid, cid, [ids[("lab", i)] for i in (1, 2, 3)])
    async with sf() as s:
        st = {i: (await s.get(PlanItem, ids[("lab", i)])).status for i in (1, 2, 3)}
    assert st == {1: "draft", 2: "failed", 3: "draft"}
    assert any("Failed" in m for m in msgs) and any("1 failed" in m for m in msgs)


async def test_feedback_loop_acceptance(env):
    sf, uid, cid, ids = env
    llm = make_llm()
    lab1, lab2 = ids[("lab", 1)], ids[("lab", 2)]
    await gen(sf, llm, uid, cid, [lab1, lab2])

    # 1. feedback on lab1 -> rules stored, new version
    async with sf() as s:
        out = await fb.process_feedback(s, llm, course_id=cid, item_id=lab1, user_id=uid, raw_texts=["Use Java for everything"], item_desc="lab 1")
        assert out.rules[0].item_id is None  # classified as course rule
    await gen(sf, llm, uid, cid, [lab1])
    async with sf() as s:
        assert (await s.get(PlanItem, lab1)).current_version == 2

    # 2. regenerating a DIFFERENT item reflects the stored course rule
    llm.providers[0].calls.clear()
    await gen(sf, llm, uid, cid, [lab2])
    assert any("Use Java for everything" in c for c in llm.providers[0].calls)

    # 3. contradicting feedback deactivates the older rule and reports replacement
    async with sf() as s:
        out = await fb.process_feedback(s, llm, course_id=cid, item_id=lab2, user_id=uid, raw_texts=["Use Python instead"], item_desc="lab 2")
        assert len(out.superseded) == 1
        old, new = out.superseded[0]
        assert old.rule_text == "Use Java for everything" and not old.active and old.superseded_by == new.id
        course_rules, _ = await fb.active_rules(s, cid, lab2)
        assert [r.rule_text for r in course_rules] == ["Use Python instead"]

    # 4. approve, then feedback -> new version, status back to draft, history keeps both
    async with sf() as s:
        (await s.get(PlanItem, lab1)).status = "approved"
        await s.commit()
    async with sf() as s:
        await fb.process_feedback(s, llm, course_id=cid, item_id=lab1, user_id=uid, raw_texts=["make it harder"], item_desc="lab 1")
    await gen(sf, llm, uid, cid, [lab1])
    async with sf() as s:
        item = await s.get(PlanItem, lab1)
        assert item.status == "draft" and item.current_version == 3
        assert [v.version for v in await repo.list_versions(s, lab1)] == [1, 2, 3]

    # 5. item-pinned correction survives regeneration of that item and does not leak
    async with sf() as s:
        out = await fb.process_feedback(s, llm, course_id=cid, item_id=lab1, user_id=uid, raw_texts=["Q3 answer should be 42"], item_desc="lab 1")
        assert out.rules[0].item_id == lab1
    llm.providers[0].calls.clear()
    await gen(sf, llm, uid, cid, [lab1])
    assert any("Q3 answer should be 42" in c for c in llm.providers[0].calls)
    llm.providers[0].calls.clear()
    await gen(sf, llm, uid, cid, [ids[("lab", 3)], lab2])
    assert not any("Q3 answer should be 42" in c for c in llm.providers[0].calls)


async def test_busy_providers_keep_job_queued(env):
    sf, uid, cid, ids = env
    status, msgs = await gen(sf, make_llm(Scripted(busy=True)), uid, cid, [ids[("lab", 1)]])
    assert status == "busy" and any("rate-limited" in m for m in msgs)
    async with sf() as s:
        j = (await s.execute(select(Job))).scalar_one()
        assert j.status == "queued" and j.payload["not_before"] > 0
        assert (await s.get(PlanItem, ids[("lab", 1)])).status == "planned"
        assert await repo.claim_next_job(s) is None  # not runnable until backoff elapses


async def test_restart_requeues_and_no_duplicate_versions(env):
    sf, uid, cid, ids = env
    lab1, lab2 = ids[("lab", 1)], ids[("lab", 2)]
    async with sf() as s:
        j = await repo.create_job(s, cid, "generate", {"item_ids": [lab1, lab2], "chat_id": 1}, uid)
        jid = j.id
    llm = make_llm()
    # Simulate: process died mid-job after lab1 finished, while lab2 was 'generating'.
    async with sf() as s:
        job = await repo.claim_next_job(s)
        item = await s.get(PlanItem, lab1)
        await jobs.process_item(s, llm, item)
        job.payload = {**job.payload, "done": [lab1]}
        (await s.get(PlanItem, lab2)).status = "generating"
        await s.commit()
    async with sf() as s:  # --- "boot" ---
        assert await repo.requeue_running_jobs(s) == 1
        assert (await s.get(PlanItem, lab2)).status == "planned"
        claimed = await repo.claim_next_job(s)
    await JobRunner(sf, llm).run_job(claimed.id)
    async with sf() as s:
        n = (await s.execute(select(ItemVersion).where(ItemVersion.item_id == lab1))).scalars().all()
        assert len(n) == 1  # lab1 not regenerated
        assert (await s.get(PlanItem, lab2)).status == "draft"
