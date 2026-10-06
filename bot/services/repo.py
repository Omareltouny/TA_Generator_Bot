"""Thin async DB helpers shared by handlers and the job worker."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (Course, FeedbackRule, ItemVersion, Job, Material, PlanItem, Template, TypeFormat, UserState)
from bot.services.planner import Draft


# ---- user state ---------------------------------------------------------
async def get_state(s: AsyncSession, user_id: int) -> UserState:
    st = await s.get(UserState, user_id)
    if st is None:
        st = UserState(user_id=user_id, data={})
        s.add(st)
        await s.flush()
    return st


async def set_state(s: AsyncSession, user_id: int, *, mode="__keep__", course_id="__keep__", data="__keep__") -> UserState:
    st = await get_state(s, user_id)
    if mode != "__keep__":
        st.mode = mode
    if course_id != "__keep__":
        st.course_id = course_id
    if data != "__keep__":
        st.data = data
    st.updated_at = datetime.now(timezone.utc)
    await s.commit()
    return st


# ---- courses / materials ------------------------------------------------
async def list_courses(s: AsyncSession) -> list[Course]:
    return list((await s.execute(select(Course).order_by(Course.id.desc()))).scalars())


async def apply_outline(course: Course, outline: dict) -> None:
    course.outline_json = outline
    course.code, course.name = outline.get("course_code"), outline.get("course_name")
    course.term, course.language_hint = outline.get("term"), outline.get("language_hint")


async def add_material(s: AsyncSession, course_id: int, kind: str, filename: str, text: str | None, user_id: int, *,
                       item_type: str | None = None, is_example: bool = False, ocr_pages: int | None = None) -> Material:
    m = Material(course_id=course_id, kind=kind, filename=filename[:300], extracted_text=text, uploaded_by=user_id,
                 item_type=item_type, is_example=is_example, ocr_pages=ocr_pages)
    s.add(m)
    await s.flush()
    return m


async def material_chars(s: AsyncSession, course_id: int) -> int:
    return (await s.execute(select(func.coalesce(func.sum(func.length(Material.extracted_text)), 0))
                            .where(Material.course_id == course_id))).scalar_one()


async def material_texts(s: AsyncSession, course_id: int, kinds=("slides", "reference")) -> list[tuple[str, str]]:
    rows = (await s.execute(select(Material).where(Material.course_id == course_id, Material.kind.in_(kinds)))).scalars()
    return [(m.filename, m.extracted_text or "") for m in rows if m.extracted_text]


async def reference_materials(s: AsyncSession, course_id: int, item_type: str) -> list[Material]:
    """Past worksheets of one type, oldest first."""
    q = select(Material).where(Material.course_id == course_id, Material.kind == "reference",
                               Material.item_type == item_type).order_by(Material.id)
    return [m for m in (await s.execute(q)).scalars() if m.extracted_text]


async def get_heading_word(s: AsyncSession, course_id: int, item_type: str) -> str:
    """Required top-level heading word for a type (TypeFormat row, else Task for labs / Question otherwise)."""
    from bot.services.generator import default_heading_word
    tf = (await s.execute(select(TypeFormat).where(TypeFormat.course_id == course_id, TypeFormat.item_type == item_type))).scalar_one_or_none()
    return tf.heading_word if tf else default_heading_word(item_type)


async def set_heading_word(s: AsyncSession, course_id: int, item_type: str, word: str) -> None:
    tf = (await s.execute(select(TypeFormat).where(TypeFormat.course_id == course_id, TypeFormat.item_type == item_type))).scalar_one_or_none()
    if tf is None:
        s.add(TypeFormat(course_id=course_id, item_type=item_type, heading_word=word))
    else:
        tf.heading_word = word


# ---- plan ---------------------------------------------------------------
async def load_plan(s: AsyncSession, course_id: int) -> list[PlanItem]:
    order = {"lab": 0, "tutorial": 1, "assignment": 2}
    items = list((await s.execute(select(PlanItem).where(PlanItem.course_id == course_id))).scalars())
    return sorted(items, key=lambda i: (order.get(i.type, 9), i.seq))


def to_drafts(items: list[PlanItem]) -> list[Draft]:
    return [Draft(i.type, i.title, i.week, list(i.topics or []), i.due_date, i.weight, i.source, i.seq, i.id) for i in items]


async def save_plan(s: AsyncSession, course_id: int, drafts: list[Draft]) -> list[PlanItem]:
    """Persist a plan by diffing against existing rows (keeps ids, versions and statuses of kept items)."""
    existing = {i.id: i for i in await load_plan(s, course_id)}
    keep = {d.id for d in drafts if d.id}
    for iid, item in existing.items():
        if iid not in keep:
            await s.delete(item)
    out = []
    for d in drafts:
        item = existing.get(d.id) if d.id else None
        if item is None:
            item = PlanItem(course_id=course_id, type=d.type, title=d.title, status="planned")
            s.add(item)
        item.type, item.week, item.seq, item.title = d.type, d.week, d.seq, d.title
        item.topics, item.due_date, item.weight, item.source = d.topics, d.due_date, d.weight, d.source
        out.append(item)
    await s.commit()
    return out


# ---- versions -----------------------------------------------------------
async def latest_version_no(s: AsyncSession, item_id: int) -> int:
    return (await s.execute(select(func.coalesce(func.max(ItemVersion.version), 0))
                            .where(ItemVersion.item_id == item_id))).scalar_one()


async def add_version(s: AsyncSession, item: PlanItem, *, student_md, key_md, student_docx, key_docx,
                      provider, model, rule_ids) -> ItemVersion:
    """New version; item goes to `draft`. Caller commits (so job bookkeeping shares the transaction)."""
    v = ItemVersion(item_id=item.id, version=await latest_version_no(s, item.id) + 1, student_md=student_md,
                    key_md=key_md, student_docx=student_docx, key_docx=key_docx, llm_provider=provider,
                    llm_model=model, feedback_rule_ids=rule_ids)
    s.add(v)
    item.current_version = v.version
    item.status = "draft"
    await s.flush()
    return v


async def get_version(s: AsyncSession, item_id: int, version: int | None = None) -> ItemVersion | None:
    v = version
    if v is None:
        item = await s.get(PlanItem, item_id)
        v = item.current_version if item else 0
    return (await s.execute(select(ItemVersion).where(ItemVersion.item_id == item_id, ItemVersion.version == v))).scalar_one_or_none()


async def list_versions(s: AsyncSession, item_id: int) -> list[ItemVersion]:
    return list((await s.execute(select(ItemVersion).where(ItemVersion.item_id == item_id).order_by(ItemVersion.version))).scalars())


# ---- jobs ---------------------------------------------------------------
async def create_job(s: AsyncSession, course_id: int, kind: str, payload: dict, user_id: int) -> Job:
    j = Job(course_id=course_id, kind=kind, payload=payload, status="queued", created_by=user_id)
    s.add(j)
    await s.commit()
    return j


async def queue_regen(s: AsyncSession, batch, *, status_msg_id: int | None = None) -> Job:
    """Create the regeneration job for a resolved feedback batch (callers invoke this once, on resolution)."""
    return await create_job(s, batch.course_id, "generate",
                            {"item_ids": [batch.regen_item_id], "chat_id": batch.chat_id,
                             "status_msg_id": status_msg_id, "origin": "feedback"}, batch.user_id)


async def claim_next_job(s: AsyncSession) -> Job | None:
    """Atomically claim the oldest runnable queued job (safe even if two instances overlap on a deploy)."""
    now = datetime.now(timezone.utc).timestamp()
    for j in (await s.execute(select(Job).where(Job.status == "queued").order_by(Job.id))).scalars():
        if (j.payload or {}).get("not_before", 0) > now:
            continue
        res = await s.execute(update(Job).where(Job.id == j.id, Job.status == "queued")
                              .values(status="running", started_at=datetime.now(timezone.utc)))
        await s.commit()
        if res.rowcount == 1:
            await s.refresh(j)
            return j
    return None


async def requeue_running_jobs(s: AsyncSession) -> int:
    """Boot recovery: running -> queued; items left `generating` go back to their prior state."""
    jobs = list((await s.execute(select(Job).where(Job.status == "running"))).scalars())
    for j in jobs:
        j.status = "queued"
    stuck = list((await s.execute(select(PlanItem).where(PlanItem.status == "generating"))).scalars())
    for it in stuck:
        it.status = "draft" if it.current_version > 0 else "planned"
    await s.commit()
    return len(jobs)


async def active_rules_count(s: AsyncSession, course_id: int) -> int:
    return (await s.execute(select(func.count(FeedbackRule.id)).where(
        FeedbackRule.course_id == course_id, FeedbackRule.status == "active"))).scalar_one()


async def list_templates(s: AsyncSession) -> list[Template]:
    return list((await s.execute(select(Template).order_by(Template.id))).scalars())
