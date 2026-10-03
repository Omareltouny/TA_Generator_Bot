"""Feedback -> atomic rules (course-level or item-pinned), latest-wins conflict resolution, retrieval (spec 6.6)."""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import FeedbackMessage, FeedbackRule

EXTRACT_SYSTEM = (
    "You turn a teaching assistant's free-text feedback about a generated course handout into atomic, reusable rules. "
    "Return JSON: {\"rules\": [{\"text\": str, \"scope\": \"course\"|\"item\"}]}. "
    "scope=course for style/format/difficulty/content preferences that should apply to every item in the course "
    "(e.g. 'use C++ not Java', 'fewer proofs', 'add marks on every question'). "
    "scope=item for corrections tied to this one item (e.g. 'Q3 answer should be 42', 'question 2 is too long'). "
    "Write each rule as a clear imperative sentence that makes sense without the original message. "
    "Do not invent rules the feedback does not contain.")

CONFLICT_SYSTEM = (
    "You compare NEW rules with EXISTING active rules for a course. A new rule supersedes an existing rule only if they "
    "contradict each other or the new one replaces it (e.g. existing 'use Java', new 'use Python'). Rules about different "
    "things do not conflict. Return JSON: {\"supersedes\": [{\"new\": <index of new rule>, \"old\": <id of existing rule>}]}.")


@dataclass
class NewRule:
    text: str
    scope: str  # course | item


@dataclass
class Outcome:
    rules: list[FeedbackRule] = field(default_factory=list)
    superseded: list[tuple[FeedbackRule, FeedbackRule]] = field(default_factory=list)  # (old, new)
    overrides: list[tuple[FeedbackRule, FeedbackRule]] = field(default_factory=list)   # (pinned new, course rule it overrides here)


# ---- pure parsing -------------------------------------------------------
def parse_rules(data: dict, allow_item: bool = True) -> list[NewRule]:
    raw = data.get("rules")
    if not isinstance(raw, list):
        raise ValueError("'rules' must be a list")
    out = []
    for r in raw:
        text = str(r.get("text", "")).strip() if isinstance(r, dict) else ""
        if not text:
            continue
        scope = "item" if (r.get("scope") == "item" and allow_item) else "course"
        out.append(NewRule(text, scope))
    return out


def parse_supersedes(data: dict, n_new: int, valid_old: set[int]) -> list[tuple[int, int]]:
    pairs = []
    for p in data.get("supersedes") or []:
        try:
            n, o = int(p["new"]), int(p["old"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= n < n_new and o in valid_old and (n, o) not in pairs:
            pairs.append((n, o))
    return pairs


# ---- LLM steps ----------------------------------------------------------
async def extract_rules(llm, raw_text: str, item_desc: str | None) -> list[NewRule]:
    prompt = (f"Item the feedback is about: {item_desc or '(whole course)'}\n\nFEEDBACK:\n{raw_text}")
    data, _ = await llm.complete_json(prompt, system=EXTRACT_SYSTEM, validate=lambda d: parse_rules(d))
    return parse_rules(data, allow_item=item_desc is not None)


async def find_conflicts(llm, new: list[NewRule], existing: list[FeedbackRule]) -> list[tuple[int, int]]:
    if not existing or not new:
        return []
    prompt = ("EXISTING ACTIVE RULES:\n" + "\n".join(f"[{r.id}] {r.rule_text}" for r in existing) +
              "\n\nNEW RULES:\n" + "\n".join(f"[{i}] {r.text}" for i, r in enumerate(new)))
    data, _ = await llm.complete_json(prompt, system=CONFLICT_SYSTEM, validate=lambda d: None)
    return parse_supersedes(data, len(new), {r.id for r in existing})


# ---- DB -----------------------------------------------------------------
async def active_rules(s: AsyncSession, course_id: int, item_id: int | None = None) -> tuple[list[FeedbackRule], list[FeedbackRule]]:
    """(course rules, rules pinned to item_id). Oldest first so later rules read as later."""
    q = select(FeedbackRule).where(FeedbackRule.course_id == course_id, FeedbackRule.active.is_(True)).order_by(FeedbackRule.id)
    rules = list((await s.execute(q)).scalars())
    return ([r for r in rules if r.item_id is None],
            [r for r in rules if item_id is not None and r.item_id == item_id])


async def process_feedback(s: AsyncSession, llm, *, course_id: int, item_id: int | None, user_id: int,
                           raw_texts: list[str], item_desc: str | None) -> Outcome:
    raw = "\n".join(t.strip() for t in raw_texts if t.strip())
    msg = FeedbackMessage(course_id=course_id, item_id=item_id, user_id=user_id, raw_text=raw)
    s.add(msg)
    await s.flush()
    new = await extract_rules(llm, raw, item_desc)
    course_rules, pinned = await active_rules(s, course_id, item_id)
    out = Outcome()
    created: list[FeedbackRule] = []
    for nr in new:
        r = FeedbackRule(course_id=course_id, item_id=item_id if nr.scope == "item" else None,
                         rule_text=nr.text, source_message_id=msg.id, active=True)
        s.add(r)
        created.append(r)
    await s.flush()
    # Latest wins, same scope only. A pinned rule never deactivates a course rule (it would break other items);
    # it simply takes precedence for its own item, and we report which course rule it overrides.
    for scope_item in {nr.item_id for nr in created}:
        mine = [(i, c) for i, c in enumerate(created) if c.item_id == scope_item]
        pool = [r for r in (course_rules if scope_item is None else pinned)]
        for local_new, old_id in await find_conflicts(llm, [NewRule(c.rule_text, "") for _, c in mine], pool):
            old, newer = next(r for r in pool if r.id == old_id), mine[local_new][1]
            old.active, old.superseded_by = False, newer.id
            out.superseded.append((old, newer))
    pinned_new = [c for c in created if c.item_id is not None]
    if pinned_new and course_rules:
        for local_new, old_id in await find_conflicts(llm, [NewRule(c.rule_text, "") for c in pinned_new], course_rules):
            out.overrides.append((next(r for r in course_rules if r.id == old_id), pinned_new[local_new]))
    out.rules = created
    await s.commit()
    return out


async def set_scope(s: AsyncSession, rule_id: int, item_id: int | None) -> FeedbackRule | None:
    r = await s.get(FeedbackRule, rule_id)
    if r:
        r.item_id = item_id
        await s.commit()
    return r


async def deactivate(s: AsyncSession, rule_id: int) -> FeedbackRule | None:
    r = await s.get(FeedbackRule, rule_id)
    if r:
        r.active = False
        await s.commit()
    return r


async def edit_rule(s: AsyncSession, rule_id: int, text: str) -> FeedbackRule | None:
    r = await s.get(FeedbackRule, rule_id)
    if r:
        r.rule_text = text.strip()
        await s.commit()
    return r
