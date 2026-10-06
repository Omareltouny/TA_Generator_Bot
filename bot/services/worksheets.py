"""Past worksheets -> editable format spec (type-scope rules) + style examples (rewrite spec 8).

Flow: the user uploads PDFs for a type -> `build_format_spec` asks the LLM to describe ONLY format/style -> the
proposal is stored as `pending/spec_review` rules (nothing applies yet) in a RuleBatch(kind="spec") together with the
proposed heading word -> the user reviews/edits/deletes -> `approve_spec` checks the rules like any new rule
(blocking clashes become conflict cards) and activates the rest. Nothing is applied or replaced without that approval.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import FeedbackRule, Material, RuleBatch
from bot.services import repo, rules
from bot.services.generator import HEADING_WORDS
from bot.services.rules import NewRule

SPEC_MAX_WORKSHEETS = 3
SPEC_MAX_CHARS = 8000
SPEC_MAX_RULES = 12
ASPECTS = ("numbering", "question_style", "structure", "marks", "difficulty", "code", "math", "answer_key", "length", "other")

FORMAT_SYSTEM = """You analyze previous worksheets of one type from a university course and describe ONLY their format and style, never their subject matter.
Return JSON: {"heading_word": "Question"|"Task"|"Problem"|"Exercise", "rules": [{"text": str, "aspect": str}]}.
"heading_word" is the word the worksheets use for a top-level numbered item (closest match of the four).
"aspect" is one of: numbering, question_style, structure, marks, difficulty, code, math, answer_key, length, other.
Write at most 12 rules. Each rule is ONE imperative sentence that is concrete and checkable (what to include, in what order, how a question is phrased, how marks are shown).
Describe patterns shared by the worksheets. If they disagree on something, omit it. Do not mention topics, specific numbers or specific questions."""


# ---- pure ------------------------------------------------------------------
def parse_format_spec(data: dict) -> tuple[str | None, list[NewRule]]:
    """LLM JSON -> (heading_word or None, up to 12 distinct rules with a known aspect)."""
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise ValueError("'rules' must be a list")
    hw = str(data.get("heading_word") or "").strip().capitalize()
    hw = hw if hw in HEADING_WORDS else None
    out: list[NewRule] = []
    seen: set[str] = set()
    for r in data["rules"]:
        text = str(r.get("text", "")).strip() if isinstance(r, dict) else ""
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        aspect = str(r.get("aspect") or "other").strip().lower()
        out.append(NewRule(text, "type", aspect=aspect if aspect in ASPECTS else "other"))
        if len(out) == SPEC_MAX_RULES:
            break
    return hw, out


def pick_spec_inputs(worksheets: list[Material]) -> list[tuple[str, str]]:
    """Up to 3 worksheets, examples first then newest, each capped at 8,000 characters."""
    ordered = sorted(worksheets, key=lambda m: (not m.is_example, -m.id))
    return [(m.filename, (m.extracted_text or "")[:SPEC_MAX_CHARS]) for m in ordered[:SPEC_MAX_WORKSHEETS]]


def group_by_aspect(rows: list[FeedbackRule]) -> list[tuple[str, list[FeedbackRule]]]:
    out: dict[str, list[FeedbackRule]] = {}
    for r in rows:
        out.setdefault(r.aspect or "other", []).append(r)
    return sorted(out.items(), key=lambda kv: ASPECTS.index(kv[0]) if kv[0] in ASPECTS else len(ASPECTS))


# ---- DB / LLM --------------------------------------------------------------
async def open_spec_batch(s: AsyncSession, course_id: int, item_type: str) -> RuleBatch | None:
    """An unreviewed proposal for this type, if one exists."""
    q = (select(RuleBatch).join(FeedbackRule, FeedbackRule.batch_id == RuleBatch.id)
         .where(RuleBatch.course_id == course_id, RuleBatch.kind == "spec", RuleBatch.item_type == item_type,
                FeedbackRule.status == "pending", FeedbackRule.pending_reason == "spec_review").order_by(RuleBatch.created_at.desc()))
    return (await s.execute(q)).scalars().first()


async def spec_rules(s: AsyncSession, batch_id: str, status: str = "pending") -> list[FeedbackRule]:
    q = select(FeedbackRule).where(FeedbackRule.batch_id == batch_id, FeedbackRule.status == status).order_by(FeedbackRule.id)
    if status == "pending":
        q = q.where(FeedbackRule.pending_reason == "spec_review")
    return list((await s.execute(q)).scalars())


async def build_format_spec(s: AsyncSession, llm, *, course_id: int, user_id: int, chat_id: int, item_type: str) -> RuleBatch:
    """Ask the LLM for the format of the type's worksheets and store it as a pending proposal.
    Raises ValueError (no worksheets / unusable reply) or AllProvidersBusy; nothing is stored in either case."""
    worksheets = await repo.reference_materials(s, course_id, item_type)
    if not worksheets:
        raise ValueError(f"no {item_type} worksheets uploaded yet")
    inputs = pick_spec_inputs(worksheets)
    prompt = (f"Item type: {item_type}\n\n" + "\n\n".join(f'<worksheet source="{n}">\n{t}\n</worksheet>' for n, t in inputs))
    data, _ = await llm.complete_json(prompt, system=FORMAT_SYSTEM, validate=lambda d: parse_format_spec(d))
    heading, new = parse_format_spec(data)
    if not new:
        raise ValueError("the worksheets did not yield any format rule")
    batch = await rules.create_batch(s, course_id=course_id, user_id=user_id, kind="spec", chat_id=chat_id,
                                     item_type=item_type, heading_word=heading or await repo.get_heading_word(s, course_id, item_type))
    for nr in new:
        s.add(FeedbackRule(course_id=course_id, scope="type", item_type=item_type, rule_text=nr.text, status="pending",
                           pending_reason="spec_review", origin="worksheet", aspect=nr.aspect, batch_id=batch.id))
    await s.commit()
    return batch


async def previous_worksheet_rules(s: AsyncSession, course_id: int, item_type: str, except_batch: str) -> list[FeedbackRule]:
    q = select(FeedbackRule).where(FeedbackRule.course_id == course_id, FeedbackRule.scope == "type", FeedbackRule.item_type == item_type,
                                   FeedbackRule.origin == "worksheet", FeedbackRule.status == "active",
                                   FeedbackRule.batch_id != except_batch).order_by(FeedbackRule.id)
    return list((await s.execute(q)).scalars())


async def replace_previous_spec(s: AsyncSession, course_id: int, item_type: str, except_batch: str) -> int:
    """Explicit user choice ("Replace previous spec"): disable the older worksheet-origin rules of the type.
    Flushed, not committed: it commits together with `approve_spec`, so a busy LLM leaves everything untouched."""
    old = await previous_worksheet_rules(s, course_id, item_type, except_batch)
    for r in old:
        r.status, r.disabled_reason = "disabled", "replaced by new worksheet spec"
    await s.flush()
    return len(old)


async def approve_spec(s: AsyncSession, llm, batch: RuleBatch):
    """Activate the proposal: set the heading word, run the normal conflict check, activate non-clashing rules.
    Raises AllProvidersBusy with nothing changed."""
    pending = await spec_rules(s, batch.id)
    if batch.heading_word and batch.item_type:
        await repo.set_heading_word(s, batch.course_id, batch.item_type, batch.heading_word)
    return await rules.activate_spec_rules(s, llm, batch, pending)


async def cancel_spec(s: AsyncSession, batch: RuleBatch) -> None:
    """Explicit Cancel: drop the unreviewed proposal (it was never active)."""
    for r in await spec_rules(s, batch.id):
        await s.delete(r)
    batch.resolved = True
    await s.commit()


async def set_proposed_heading(s: AsyncSession, batch: RuleBatch, word: str) -> None:
    if word not in HEADING_WORDS:
        raise ValueError("unknown heading word")
    batch.heading_word = word
    await s.commit()


# ---- examples ----------------------------------------------------------------
async def worksheets_by_type(s: AsyncSession, course_id: int) -> dict[str, list[Material]]:
    out = {t: [] for t in rules.ITEM_TYPES}
    for t in rules.ITEM_TYPES:
        out[t] = await repo.reference_materials(s, course_id, t)
    return out


async def toggle_example(s: AsyncSession, material_id: int, limit: int) -> tuple[Material | None, Material | None]:
    """Toggle `is_example`. Turning one on at the limit swaps off the oldest example of that type.
    Returns (material, swapped_off_material_or_None)."""
    m = await s.get(Material, material_id)
    if m is None or m.kind != "reference" or not m.item_type:
        return None, None
    if m.is_example:
        m.is_example = False
        await s.commit()
        return m, None
    on = [x for x in await repo.reference_materials(s, m.course_id, m.item_type) if x.is_example and x.id != m.id]
    swapped = None
    while on and len(on) >= max(limit, 1):
        swapped = on.pop(0)  # lowest id = oldest
        swapped.is_example = False
    m.is_example = True
    await s.commit()
    return m, swapped
