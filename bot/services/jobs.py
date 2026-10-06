"""DB-backed job queue + in-process async worker (spec 4, 6.4, 10).

Job payload: {item_ids, chat_id, status_msg_id, done:[ids], failed:{id:reason}, not_before, origin, notified_busy}
Idempotence: an item's version insert and the job's `done` bookkeeping commit in ONE transaction, so a crash
either keeps both or neither; on resume, `done` items are skipped (no duplicate versions).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import Course, Job, Material, PlanItem, Template
from bot.services import generator, materials, renderer, repo, rules
from bot.services.llm_router import AllProvidersBusy

log = logging.getLogger("jobs")
GENERIC_TITLE = __import__("re").compile(r"^(worksheet|lab|tutorial|assignment)\s*\d*$", __import__("re").I)


async def process_item(s: AsyncSession, llm, item: PlanItem, *, examples_per_type: int = 2,
                       example_max_chars: int = generator.DEFAULT_EXAMPLE_MAX_CHARS):
    """Generate + render one item and add a new version (not committed). Raises on failure.

    Rules come fresh from the DB every time (`rules.applicable_rules`): regeneration can never use a subset of the
    active rules. Style examples are the type's worksheets flagged `is_example`; BM25 excerpts still ground topics
    from slides and from non-example worksheets of the same type.
    """
    course = await s.get(Course, item.course_id)
    plan = await repo.load_plan(s, item.course_id)
    same = [p for p in plan if p.type == item.type]
    idx = same.index(next(p for p in same if p.id == item.id))
    neighbours = [dict(type=p.type, seq=p.seq, week=p.week, title=p.title, topics=p.topics or [])
                  for p in same[max(0, idx - 1):idx] + same[idx + 1:idx + 2]]
    query = " ".join([item.title, *(item.topics or [])])
    worksheets = await repo.reference_materials(s, course.id, item.type)
    examples = [(m.filename, m.extracted_text) for m in worksheets if m.is_example][:examples_per_type]
    others = [(m.filename, m.extracted_text) for m in worksheets if not m.is_example]
    excerpts = materials.retrieve(await repo.material_texts(s, course.id, ("slides",)), query) + materials.retrieve(others, query, k=2)
    applicable = await rules.applicable_rules(s, course.id, item)
    heading_word = await repo.get_heading_word(s, course.id, item.type)
    res = await generator.generate_item(
        llm, {"outline": course.outline_json or {}},
        dict(type=item.type, seq=item.seq, title=item.title, week=item.week, topics=item.topics or [],
             weight=item.weight, due_date=item.due_date),
        neighbours, excerpts, examples, applicable, heading_word, example_max_chars)
    if item.current_version == 0 and GENERIC_TITLE.match(item.title.strip()) and res.title:
        item.title = f"{item.title.strip()}: {res.title}"[:290]
    template = await s.get(Template, course.template_id) if course.template_id else None
    meta = renderer.DocMeta(course.code or "", course.name or "", item.type, item.seq, item.week, item.title)
    sd, kd = await renderer.render_pair(res.student_md, res.key_md, meta, logo=course.logo_blob,
                                        template=template.data if template else None)
    return await repo.add_version(s, item, student_md=res.student_md, key_md=res.key_md, student_docx=sd, key_docx=kd,
                                  provider=res.provider, model=res.model, rule_ids=applicable.ids)


# ---- status message text (rewrite spec 10.2) ----------------------------------
@dataclass
class StatusItem:
    label: str                 # e.g. "Lab 3"
    state: str = "queued"      # queued | writing | done | failed


COLLAPSE_AFTER = 12
_STATE_TEXT = {"queued": "", "writing": "writing...", "done": "done", "failed": "failed"}


def item_label(item) -> str:
    return f"{item.type.title()} {item.seq}"


def kind_label(items) -> str:
    types = {i.type for i in items}
    return f"{next(iter(types))}s" if len(types) == 1 else "items"


def render_status(items: list[StatusItem], *, kind: str = "items", rules_line: str = "", notices=(), final: str | None = None) -> str:
    """The single live status message of a job. Pure; the runner passes the current state every time."""
    total = len(items)
    done = sum(1 for i in items if i.state == "done")
    failed = sum(1 for i in items if i.state == "failed")
    if final is not None:
        head = final
    elif total == 1:
        head = f"Generating {items[0].label}..."
    else:
        head = f"Generating {kind}: {done + failed}/{total}"
    lines = [head]
    if total > 1:
        if total <= COLLAPSE_AFTER:
            lines.append(" | ".join(f"{i.label} {_STATE_TEXT[i.state]}".strip() for i in items))
        else:
            now = next((i.label for i in items if i.state == "writing"), "-")
            left = sum(1 for i in items if i.state == "queued")
            lines.append(f"done {done} | failed {failed} | now: {now} | left: {left}")
    if rules_line:
        lines.append(rules_line)
    lines.extend(notices)
    return "\n".join(lines)


class JobRunner:
    """Runs queued generation jobs one item at a time (rewrite spec 10).

    Telegram surface: ONE live status message per job (edited through `status`, which the app wires to a throttled
    `LiveMessage`), items delivered one by one through `deliver(chat_id, item_id, single=...)`, and - only for jobs
    with more than one item - a final message through `notify` with [Review items] [Retry failed].
    """

    def __init__(self, sf: async_sessionmaker, llm, *, deliver: Callable[..., Awaitable] | None = None,
                 notify: Callable[..., Awaitable] | None = None, status: Callable[..., Awaitable] | None = None, cfg=None):
        self.sf, self.llm = sf, llm
        self.deliver = deliver      # async (chat_id, item_id, single=bool)
        self.notify = notify        # async (chat_id, text, reply_markup=None) - final message of multi-item jobs
        self.status = status        # async (chat_id, message_id, text, markup=None, force=False)
        self.examples_per_type = getattr(cfg, "examples_per_type", 2)
        self.example_max_chars = getattr(cfg, "example_max_chars", generator.DEFAULT_EXAMPLE_MAX_CHARS)
        self._stop = False
        self.tick_hooks: list[Callable[[], Awaitable]] = []

    async def _say(self, chat_id, text, **kw):
        if self.notify and chat_id:
            try:
                await self.notify(chat_id, text, **kw)
            except Exception:
                log.exception("notify failed")

    async def _push(self, p: dict, labels: list[tuple[int, str]], kind: str, rules_line: str, *, writing: int | None = None,
                    notices=(), final: str | None = None, markup=None, force: bool = False):
        """Edit the job's single status message from the payload state. Never raises."""
        chat, mid = p.get("chat_id"), p.get("status_msg_id")
        if not (self.status and chat and mid):
            return
        done, failed = set(p.get("done", [])), set(p.get("failed", {}))
        items = [StatusItem(lb, "done" if i in done else "failed" if str(i) in failed else "writing" if i == writing else "queued")
                 for i, lb in labels]
        fails = [f"{lb} failed: {p['failed'][str(i)][:120]}" for i, lb in labels if str(i) in p.get("failed", {})][-3:]
        text = render_status(items, kind=kind, rules_line=rules_line, notices=[*fails, *notices], final=final)
        try:
            await self.status(chat, mid, text, markup, force)
        except Exception:
            log.exception("status update failed")

    async def run_job(self, job_id: int) -> str:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        async with self.sf() as s:
            job = await s.get(Job, job_id)
            p = dict(job.payload or {})
            p.pop("busy_notice", None)
            chat = p.get("chat_id")
            ids = p.get("item_ids", [])
            todo = [i for i in ids if i not in p.get("done", []) and str(i) not in p.get("failed", {})]
            plan_items = [x for x in [await s.get(PlanItem, i) for i in ids] if x]
            labels = [(x.id, item_label(x)) for x in plan_items]
            kind = kind_label(plan_items)
            rules_line = await rules.in_use_summary(s, plan_items[0].course_id, plan_items) if plan_items else ""
            single = len(ids) == 1
            for item_id in todo:
                await s.refresh(job)
                if job.status == "cancelled":
                    await self._push(p, labels, kind, rules_line, final="Cancelled.", force=True)
                    return "cancelled"
                item = await s.get(PlanItem, item_id)
                if item is None:
                    continue
                prev_status = item.status
                item.status = "generating"
                await s.commit()
                n_done = len(p.get("done", [])) + len(p.get("failed", {}))
                await self._push(p, labels, kind, rules_line, writing=item_id)
                try:
                    log.info("generate job=%s course=%s item=%s", job.id, item.course_id, item.id)
                    await process_item(s, self.llm, item, examples_per_type=self.examples_per_type,
                                       example_max_chars=self.example_max_chars)
                    p.setdefault("done", []).append(item_id)
                    job.payload, job.progress = dict(p), n_done + 1
                    await s.commit()  # version + bookkeeping atomically
                    await self._push(p, labels, kind, rules_line)
                    if self.deliver:
                        try:
                            await self.deliver(chat, item_id, single=single)
                        except Exception:
                            log.exception("delivery failed item=%s", item_id)
                except AllProvidersBusy as e:
                    await s.rollback()
                    item = await s.get(PlanItem, item_id)
                    item.status = prev_status if prev_status != "generating" else "planned"
                    job = await s.get(Job, job_id)
                    wait = max(15, e.retry_after)
                    p["not_before"] = time.time() + wait
                    job.payload, job.status = dict(p), "queued"
                    await s.commit()
                    await self._push(p, labels, kind, rules_line, force=True,
                                     notices=[f"AI providers are rate-limited. This job stays queued and resumes by itself in ~{int(e.retry_after)}s."])
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
                    await self._push(p, labels, kind, rules_line)
            await s.refresh(job)
            if job.status != "cancelled":
                job.status, job.finished_at = "done", datetime.now(timezone.utc)
                await s.commit()
            failed = p.get("failed", {})
            n_ok = len(p.get("done", []))
            if single:
                if failed:
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Retry", callback_data=f"rf:{job.id}")]])
                    await self._push(p, labels, kind, rules_line, final=f"{labels[0][1]} failed.", markup=kb, force=True)
                else:
                    ver = (await s.get(PlanItem, ids[0])).current_version
                    await self._push(p, labels, kind, rules_line, final=f"{labels[0][1]} v{ver} ready", force=True)
            else:
                final = f"Done: {n_ok} generated" + (f", {len(failed)} failed." if failed else ".")
                await self._push(p, labels, kind, rules_line, final=final, force=True)
                rows = [InlineKeyboardButton("Review items", callback_data="il:all:all:0")]
                if failed:
                    rows.append(InlineKeyboardButton("Retry failed", callback_data=f"rf:{job.id}"))
                await self._say(chat, final, reply_markup=InlineKeyboardMarkup([rows]))
            return "done"

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
