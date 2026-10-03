"""DB-backed job queue + in-process async worker (spec 4, 6.4, 10).

Job payload: {item_ids, chat_id, progress_msg_id, done:[ids], failed:{id:reason}, not_before, origin, notified_busy}
Idempotence: an item's version insert and the job's `done` bookkeeping commit in ONE transaction, so a crash
either keeps both or neither; on resume, `done` items are skipped (no duplicate versions).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import Course, Job, Material, PlanItem, Template
from bot.services import feedback_store, generator, materials, renderer, repo
from bot.services.llm_router import AllProvidersBusy

log = logging.getLogger("jobs")
GENERIC_TITLE = __import__("re").compile(r"^(worksheet|lab|tutorial|assignment)\s*\d*$", __import__("re").I)


async def process_item(s: AsyncSession, llm, item: PlanItem):
    """Generate + render one item and add a new version (not committed). Raises on failure."""
    course = await s.get(Course, item.course_id)
    plan = await repo.load_plan(s, item.course_id)
    same = [p for p in plan if p.type == item.type]
    idx = same.index(next(p for p in same if p.id == item.id))
    neighbours = [dict(type=p.type, seq=p.seq, week=p.week, title=p.title, topics=p.topics or [])
                  for p in same[max(0, idx - 1):idx] + same[idx + 1:idx + 2]]
    sources = await repo.material_texts(s, course.id)
    query = " ".join([item.title, *(item.topics or [])])
    excerpts = materials.retrieve(sources, query) + materials.retrieve(await repo.material_texts(s, course.id, ("reference",)), query, k=2)
    crules, irules = await feedback_store.active_rules(s, course.id, item.id)
    res = await generator.generate_item(
        llm, {"outline": course.outline_json or {}},
        dict(type=item.type, seq=item.seq, title=item.title, week=item.week, topics=item.topics or [],
             weight=item.weight, due_date=item.due_date),
        neighbours, excerpts, [r.rule_text for r in crules], [r.rule_text for r in irules])
    if item.current_version == 0 and GENERIC_TITLE.match(item.title.strip()) and res.title:
        item.title = f"{item.title.strip()}: {res.title}"[:290]
    template = await s.get(Template, course.template_id) if course.template_id else None
    meta = renderer.DocMeta(course.code or "", course.name or "", item.type, item.seq, item.week, item.title)
    sd, kd = await renderer.render_pair(res.student_md, res.key_md, meta, logo=course.logo_blob,
                                        template=template.data if template else None)
    return await repo.add_version(s, item, student_md=res.student_md, key_md=res.key_md, student_docx=sd, key_docx=kd,
                                  provider=res.provider, model=res.model, rule_ids=[r.id for r in crules + irules])


class JobRunner:
    def __init__(self, sf: async_sessionmaker, llm, *, deliver: Callable[..., Awaitable] | None = None,
                 notify: Callable[..., Awaitable] | None = None, progress: Callable[..., Awaitable] | None = None):
        self.sf, self.llm = sf, llm
        self.deliver = deliver      # async (chat_id, item_id)
        self.notify = notify        # async (chat_id, text, reply_markup=None)
        self.progress = progress    # async (chat_id, message_id, text)
        self._stop = False
        self.tick_hooks: list[Callable[[], Awaitable]] = []

    async def _say(self, chat_id, text, **kw):
        if self.notify and chat_id:
            try:
                await self.notify(chat_id, text, **kw)
            except Exception:
                log.exception("notify failed")

    async def run_job(self, job_id: int) -> str:
        async with self.sf() as s:
            job = await s.get(Job, job_id)
            p = dict(job.payload or {})
            chat = p.get("chat_id")
            todo = [i for i in p.get("item_ids", []) if i not in p.get("done", []) and str(i) not in p.get("failed", {})]
            total = len(p.get("item_ids", []))
            for item_id in todo:
                await s.refresh(job)
                if job.status == "cancelled":
                    await self._say(chat, "Job cancelled.")
                    return "cancelled"
                item = await s.get(PlanItem, item_id)
                if item is None:
                    continue
                prev_status = item.status
                item.status = "generating"
                await s.commit()
                n_done = len(p.get("done", [])) + len(p.get("failed", {}))
                if self.progress and p.get("progress_msg_id"):
                    try:
                        await self.progress(chat, p["progress_msg_id"], f"Generating {n_done + 1}/{total}: {item.type} {item.seq} - {item.title}")
                    except Exception:
                        pass
                try:
                    log.info("generate job=%s course=%s item=%s", job.id, item.course_id, item.id)
                    await process_item(s, self.llm, item)
                    p.setdefault("done", []).append(item_id)
                    job.payload, job.progress = dict(p), n_done + 1
                    await s.commit()  # version + bookkeeping atomically
                    if self.deliver:
                        try:
                            await self.deliver(chat, item_id)
                        except Exception:
                            log.exception("delivery failed item=%s", item_id)
                except AllProvidersBusy as e:
                    await s.rollback()
                    item = await s.get(PlanItem, item_id)
                    item.status = prev_status if prev_status != "generating" else "planned"
                    job = await s.get(Job, job_id)
                    if p.get("notified_busy") is None:
                        await self._say(chat, f"All AI providers are rate-limited right now. Your job stays queued and resumes automatically in ~{int(e.retry_after)}s.")
                    p["notified_busy"] = True
                    p["not_before"] = time.time() + max(15, e.retry_after)
                    job.payload, job.status = dict(p), "queued"
                    await s.commit()
                    return "busy"
                except Exception as e:
                    log.exception("item failed item=%s", item_id)
                    await s.rollback()
                    item = await s.get(PlanItem, item_id)
                    job = await s.get(Job, job_id)
                    item.status = "failed" if item.current_version == 0 else "draft"
                    p.setdefault("failed", {})[str(item_id)] = f"{type(e).__name__}: {str(e)[:200]}"
                    job.payload, job.progress = dict(p), n_done + 1
                    await s.commit()
                    await self._say(chat, f"Failed: {item.type} {item.seq} - {str(e)[:200]}")
            await s.refresh(job)
            if job.status != "cancelled":
                job.status, job.finished_at = "done", datetime.now(timezone.utc)
                await s.commit()
            failed = p.get("failed", {})
            await self._summary(chat, job.id, p, failed)
            return "done"

    async def _summary(self, chat, job_id, p, failed):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        text = f"Finished: {len(p.get('done', []))} generated" + (f", {len(failed)} failed." if failed else ".")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Retry failed", callback_data=f"rf:{job_id}")]]) if failed else None
        await self._say(chat, text, reply_markup=kb)

    async def run_forever(self, poll_s: float = 2.0):
        log.info("job worker started")
        while not self._stop:
            try:
                async with self.sf() as s:
                    job = await repo.claim_next_job(s)
                if job:
                    await self.run_job(job.id)
                    continue
                for hook in self.tick_hooks:
                    await hook()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker loop error")
            await asyncio.sleep(poll_s)

    def stop(self):
        self._stop = True
