"""Rules engine (TA_BOT_REWRITE_SPEC sections 3-4).

Rules have a scope (course | type | item) and a status (active | pending | disabled).
Precedence at generation time is item > type > course (see `applicable_rules`).

The one invariant that matters: **no function here changes a rule's status on its own.** A rule leaves `active`
only through an explicit user action: `resolve_conflict`, `disable_rule`, `edit_rule_text` / `change_scope` /
`restore_rule` on that same rule (which re-check it), or `delete_rule_permanently`. A new rule that clashes with an
existing one becomes `pending`; the existing rule is never touched until the user answers.

LLM calls (extraction, conflict check) go through the router; `AllProvidersBusy` propagates to the caller **before**
any rule row is written, so a failed check can never activate rules unchecked.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import FeedbackRule, PlanItem, RuleBatch

log = logging.getLogger("rules")

SCOPES = ("course", "type", "item")
ITEM_TYPES = ("lab", "tutorial", "assignment")
CHUNK = 40                  # max existing rules per conflict-check call (free-tier token limits)
MIN_REASON_CHARS = 10
BREADTH = {"course": 2, "type": 1, "item": 0}

EXTRACT_SYSTEM = """You convert a teaching assistant's free-text feedback about ONE generated course handout into atomic rules.
Return JSON: {"rules": [{"text": str, "scope": "item"|"type"|"course"}]}.
Default scope is "item": a correction to this specific handout (a wrong answer, a question that is too long or too hard, change Q3, add a topic here).
Use "type" ONLY if the feedback explicitly generalizes to all handouts of this kind ("all labs", "every lab", "from now on for labs").
Use "course" ONLY if it explicitly generalizes to everything in the course ("everywhere", "in all material", "always use Python").
When unsure, choose "item".
Each rule is ONE imperative sentence that makes sense without the original message and contains ONE requirement.
Do not invent rules the feedback does not contain. If there is no actionable instruction, return {"rules": []}."""

FIXED_SCOPE_NOTE = ("\nEvery rule has the scope given in the user message under SCOPE; set \"scope\" to exactly that value. "
                    "Still split the text into atomic rules.")

CONFLICT_SYSTEM = """You check whether NEW rules contradict EXISTING rules for the same course material.
Two rules conflict ONLY if no single handout could satisfy both: they give mutually exclusive instructions about the same aspect.
Rules about different aspects do not conflict. A more specific rule that only narrows or adds to a general one does not conflict unless the instructions are mutually exclusive.
Also flag a NEW rule as a duplicate if an existing rule already requires the same thing.
Return JSON: {"conflicts": [{"new": int, "existing": int, "reason": str}], "duplicates": [{"new": int, "existing": int}]}.
"reason" is ONE sentence naming the exact clash: what each rule demands.
Omit anything you are not sure about."""


class RuleInvariantError(ValueError):
    """scope / item_type / item_id combination is inconsistent."""


# ---- data classes ---------------------------------------------------------
@dataclass
class NewRule:
    text: str
    scope: str                      # item | type | course
    item_type: str | None = None    # only for scope == "type" (resolved by the caller if absent)
    aspect: str | None = None       # worksheet-derived rules


@dataclass
class Applicable:
    """Active rules that apply to one item, per scope, oldest first."""
    course: list[FeedbackRule] = field(default_factory=list)
    type: list[FeedbackRule] = field(default_factory=list)
    item: list[FeedbackRule] = field(default_factory=list)

    @property
    def all(self) -> list[FeedbackRule]:
        return [*self.course, *self.type, *self.item]

    @property
    def ids(self) -> list[int]:
        return [r.id for r in self.all]


@dataclass
class Clash:
    new_idx: int
    existing: FeedbackRule
    reason: str
    blocking: bool  # True only when both rules share the same scope key


@dataclass
class Override:
    rule: FeedbackRule      # the new, narrower (or equal-precedence non-blocking) rule
    existing: FeedbackRule  # the rule it overrides for the items it applies to
    reason: str


@dataclass
class AddOutcome:
    rules: list[FeedbackRule] = field(default_factory=list)       # rows kept (active or pending)
    pending: list[FeedbackRule] = field(default_factory=list)
    overrides: list[Override] = field(default_factory=list)
    duplicates: list[tuple[str, FeedbackRule]] = field(default_factory=list)  # (new text, rule already covering it)
    resolved_batch: RuleBatch | None = None  # set when this call made the batch fully resolved (caller queues regen)


# ---- pure helpers ---------------------------------------------------------
def scope_key(r: FeedbackRule) -> tuple:
    return ("course",) if r.scope == "course" else (("type", r.item_type) if r.scope == "type" else ("item", r.item_id))


def is_blocking(a: FeedbackRule, b: FeedbackRule) -> bool:
    """Same scope key => precedence cannot settle it, the user must decide."""
    return scope_key(a) == scope_key(b)


def covers(existing: FeedbackRule, new: FeedbackRule) -> bool:
    """True if `existing` applies wherever `new` would (same or broader scope), so `new` adds nothing."""
    if scope_key(existing) == scope_key(new):
        return True
    if BREADTH[existing.scope] <= BREADTH[new.scope]:
        return False
    if existing.scope == "course":
        return True
    return existing.scope == "type" and new.scope == "item"  # item's own type is checked by the pool filter


def check_invariants(r: FeedbackRule) -> None:
    if r.scope not in SCOPES:
        raise RuleInvariantError(f"unknown scope {r.scope!r}")
    if r.scope == "item" and not (r.item_id and r.item_type is None):
        raise RuleInvariantError("scope=item needs item_id and no item_type")
    if r.scope == "type" and not (r.item_type in ITEM_TYPES and r.item_id is None):
        raise RuleInvariantError("scope=type needs a valid item_type and no item_id")
    if r.scope == "course" and (r.item_type is not None or r.item_id is not None):
        raise RuleInvariantError("scope=course must have no item_type / item_id")


def scope_label(r: FeedbackRule | NewRule) -> str:
    if r.scope == "course":
        return "course"
    if r.scope == "type":
        return f"all {r.item_type}s"
    return "this item" if isinstance(r, NewRule) or r.item_id is None else f"item {r.item_id}"


def parse_rules(data: dict, *, fixed_scope: str | None = None, allow_item: bool = True,
                allow_type: bool = True) -> list[NewRule]:
    """Pure: LLM JSON -> NewRule list. Unknown/disallowed scopes degrade to the narrowest allowed one."""
    raw = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ValueError("'rules' must be a list")
    out: list[NewRule] = []
    for r in raw:
        text = str(r.get("text", "")).strip() if isinstance(r, dict) else ""
        if not text:
            continue
        if fixed_scope:
            scope = fixed_scope
        else:
            scope = r.get("scope") if r.get("scope") in SCOPES else "item"
            if scope == "type" and not allow_type:
                scope = "item"
            if scope == "item" and not allow_item:
                scope = "course"
        out.append(NewRule(text, scope))
    return out


def parse_conflicts(data: dict, n_new: int, valid_existing: set[int]) -> tuple[list[tuple[int, int, str]], list[tuple[int, int]]]:
    """Pure post-processing of the conflict-check reply.

    Drops unknown ids, conflicts with a missing/short reason, and duplicate pairs. A pair reported as both a
    conflict and a duplicate counts as a duplicate.
    """
    if not isinstance(data, dict):
        raise ValueError("reply must be a JSON object")
    for key in ("conflicts", "duplicates"):
        if data.get(key) is not None and not isinstance(data[key], list):
            raise ValueError(f"'{key}' must be a list")

    def ids(entry):
        try:
            n, e = int(entry["new"]), int(entry["existing"])
        except (KeyError, TypeError, ValueError):
            return None
        return (n, e) if 0 <= n < n_new and e in valid_existing else None

    dups: list[tuple[int, int]] = []
    for d in data.get("duplicates") or []:
        pair = ids(d) if isinstance(d, dict) else None
        if pair and pair not in dups:
            dups.append(pair)
    conflicts: list[tuple[int, int, str]] = []
    seen: set[tuple[int, int]] = set()
    for c in data.get("conflicts") or []:
        pair = ids(c) if isinstance(c, dict) else None
        reason = str(c.get("reason") or "").strip() if isinstance(c, dict) else ""
        if not pair or len(reason) < MIN_REASON_CHARS or pair in seen or pair in dups:
            continue
        seen.add(pair)
        conflicts.append((*pair, reason))
    return conflicts, dups


# ---- DB queries -----------------------------------------------------------
async def _active_rules(s: AsyncSession, course_id: int) -> list[FeedbackRule]:
    q = select(FeedbackRule).where(FeedbackRule.course_id == course_id, FeedbackRule.status == "active").order_by(FeedbackRule.id)
    return list((await s.execute(q)).scalars())


async def applicable_rules(s: AsyncSession, course_id: int, item: PlanItem) -> Applicable:
    """Active rules for one item. `pending` and `disabled` rules are never used in generation."""
    rules = await _active_rules(s, course_id)
    return Applicable(
        course=[r for r in rules if r.scope == "course"],
        type=[r for r in rules if r.scope == "type" and r.item_type == item.type],
        item=[r for r in rules if r.scope == "item" and r.item_id == item.id])


async def rule_counts(s: AsyncSession, course_id: int) -> dict:
    """{'course','lab','tutorial','assignment','item','disabled','pending'} counts for hubs and summaries."""
    rows = (await s.execute(select(FeedbackRule.scope, FeedbackRule.item_type, FeedbackRule.status, func.count())
                            .where(FeedbackRule.course_id == course_id)
                            .group_by(FeedbackRule.scope, FeedbackRule.item_type, FeedbackRule.status))).all()
    c = {"course": 0, "lab": 0, "tutorial": 0, "assignment": 0, "item": 0, "disabled": 0, "pending": 0}
    for scope, itype, status, n in rows:
        if status == "disabled":
            c["disabled"] += n
        elif status == "pending":
            c["pending"] += n
        elif scope == "type" and itype in c:
            c[itype] += n
        elif scope in ("course", "item"):
            c[scope] += n
    return c


async def count_pending(s: AsyncSession, course_id: int) -> int:
    return (await s.execute(select(func.count(FeedbackRule.id)).where(
        FeedbackRule.course_id == course_id, FeedbackRule.status == "pending"))).scalar_one()


async def pending_rules(s: AsyncSession, course_id: int, reason: str | None = None) -> list[FeedbackRule]:
    q = select(FeedbackRule).where(FeedbackRule.course_id == course_id, FeedbackRule.status == "pending").order_by(FeedbackRule.id)
    if reason:
        q = q.where(FeedbackRule.pending_reason == reason)
    return list((await s.execute(q)).scalars())


async def worksheet_formats(s: AsyncSession, course_id: int) -> dict[str, bool]:
    """Which item types have an active worksheet-derived format (for the gate summary)."""
    rows = (await s.execute(select(FeedbackRule.item_type).where(
        FeedbackRule.course_id == course_id, FeedbackRule.status == "active", FeedbackRule.origin == "worksheet",
        FeedbackRule.scope == "type").distinct())).scalars()
    have = set(rows)
    return {t: t in have for t in ITEM_TYPES}


async def summary_line(s: AsyncSession, course_id: int) -> str:
    c = await rule_counts(s, course_id)
    return f"Rules in effect: course {c['course']} | labs {c['lab']} | tutorials {c['tutorial']} | assignments {c['assignment']}"


async def in_use_summary(s: AsyncSession, course_id: int, items: list[PlanItem]) -> str:
    """'Rules in use: 2 course, 3 labs, 2 this item' for the items of a job (zero counts omitted)."""
    active = await _active_rules(s, course_id)
    parts = [(sum(1 for r in active if r.scope == "course"), "course")]
    for t in sorted({i.type for i in items}):
        parts.append((sum(1 for r in active if r.scope == "type" and r.item_type == t), f"{t}s"))
    if len(items) == 1:
        parts.append((sum(1 for r in active if r.scope == "item" and r.item_id == items[0].id), "this item"))
    shown = [f"{n} {name}" for n, name in parts if n]
    return "Rules in use: " + (", ".join(shown) if shown else "none")


# ---- LLM steps ------------------------------------------------------------
async def extract_rules(llm, raw_text: str, item_desc: str | None, item_type: str | None, *,
                        fixed_scope: str | None = None) -> list[NewRule]:
    """Feedback text -> atomic rules. With `fixed_scope` (manual rule entry) the scope is not guessed."""
    system = EXTRACT_SYSTEM + (FIXED_SCOPE_NOTE if fixed_scope else "")
    prompt = (f"Item the feedback is about: {item_desc or '(none)'}\n"
              + (f"SCOPE: {fixed_scope}\n" if fixed_scope else "") + f"\nFEEDBACK:\n{raw_text}")
    kw = dict(fixed_scope=fixed_scope, allow_item=item_desc is not None, allow_type=item_type is not None)
    data, _ = await llm.complete_json(prompt, system=system, validate=lambda d: parse_rules(d, **kw))
    return parse_rules(data, **kw)


async def _pools(s: AsyncSession, course_id: int, new: list[FeedbackRule], exclude: set[int]) -> dict[tuple, list[FeedbackRule]]:
    active = [r for r in await _active_rules(s, course_id) if r.id not in exclude]
    types = dict((await s.execute(select(PlanItem.id, PlanItem.type).where(PlanItem.course_id == course_id))).all())
    pools: dict[tuple, list[FeedbackRule]] = {}
    for n in new:
        key = (n.scope, n.item_type, n.item_id)
        if key in pools:
            continue
        if n.scope == "course":
            pool = active
        elif n.scope == "item":
            t = types.get(n.item_id)
            pool = [r for r in active if r.scope == "course" or (r.scope == "type" and r.item_type == t)
                    or (r.scope == "item" and r.item_id == n.item_id)]
        else:
            pool = [r for r in active if r.scope == "course" or (r.scope == "type" and r.item_type == n.item_type)
                    or (r.scope == "item" and types.get(r.item_id) == n.item_type)]
        pools[key] = pool
    return pools


async def check_conflicts(llm, s: AsyncSession, course_id: int, new: list[FeedbackRule], *,
                          exclude_ids: set[int] | None = None) -> tuple[list[Clash], list[tuple[int, FeedbackRule]]]:
    """Compare `new` rules (transient or persisted) with the active rules they can apply together with.

    Returns (clashes, duplicates). Rules are never modified. Pools above CHUNK rules are split and merged.
    """
    exclude = {r.id for r in new if r.id} | set(exclude_ids or ())
    pools = await _pools(s, course_id, new, exclude)
    clashes: list[Clash] = []
    dups: list[tuple[int, FeedbackRule]] = []
    groups: dict[tuple, list[int]] = {}
    for i, n in enumerate(new):
        groups.setdefault((n.scope, n.item_type, n.item_id), []).append(i)
    for key, idxs in groups.items():
        pool = pools[key]
        for start in range(0, len(pool), CHUNK):
            chunk = pool[start:start + CHUNK]
            prompt = ("EXISTING RULES:\n" + "\n".join(f"[{r.id}] ({scope_label(r)}) {r.rule_text}" for r in chunk)
                      + "\n\nNEW RULES:\n" + "\n".join(f"[{j}] ({scope_label(new[i])}) {new[i].rule_text}" for j, i in enumerate(idxs)))
            valid = {r.id for r in chunk}
            data, _ = await llm.complete_json(prompt, system=CONFLICT_SYSTEM,
                                              validate=lambda d, n_=len(idxs), v=valid: parse_conflicts(d, n_, v))
            conflicts, dup_pairs = parse_conflicts(data, len(idxs), valid)
            by_id = {r.id: r for r in chunk}
            for j, eid, reason in conflicts:
                ex = by_id[eid]
                clashes.append(Clash(idxs[j], ex, reason, is_blocking(new[idxs[j]], ex)))
            for j, eid in dup_pairs:
                dups.append((idxs[j], by_id[eid]))
    return clashes, dups


# ---- lifecycle ------------------------------------------------------------
async def create_batch(s: AsyncSession, *, course_id: int, user_id: int, kind: str, chat_id: int | None,
                       item_id: int | None = None, regen_item_id: int | None = None,
                       item_type: str | None = None, heading_word: str | None = None) -> RuleBatch:
    b = RuleBatch(course_id=course_id, user_id=user_id, kind=kind, chat_id=chat_id, item_id=item_id,
                  regen_item_id=regen_item_id, item_type=item_type, heading_word=heading_word, resolved=False)
    s.add(b)
    await s.flush()
    return b


def _build_row(batch: RuleBatch, nr: NewRule, origin: str, item: PlanItem | None, source_message_id: int | None) -> FeedbackRule:
    scope = nr.scope
    r = FeedbackRule(course_id=batch.course_id, scope=scope, rule_text=nr.text.strip(), source_message_id=source_message_id,
                     status="active", origin=origin, aspect=nr.aspect, batch_id=batch.id)
    if scope == "item":
        r.item_id = item.id if item else None
    elif scope == "type":
        r.item_type = nr.item_type or (item.type if item else None)
    check_invariants(r)
    return r


async def _maybe_resolve(s: AsyncSession, batch_id: str | None) -> RuleBatch | None:
    """Mark the batch resolved when it has no pending rule left. Returns it only on the False -> True transition,
    which is the single moment the caller may queue the regeneration."""
    if not batch_id:
        return None
    b = await s.get(RuleBatch, batch_id)
    if b is None or b.resolved:
        return None
    n = (await s.execute(select(func.count(FeedbackRule.id)).where(
        FeedbackRule.batch_id == batch_id, FeedbackRule.status == "pending"))).scalar_one()
    if n:
        return None
    b.resolved = True
    return b


def _apply_clashes(rows: list[FeedbackRule], clashes: list[Clash], out: AddOutcome, only: set[int] | None = None) -> None:
    """Blocking clash -> the NEW rule becomes pending (existing rule untouched). Non-blocking -> informational override."""
    for i, r in enumerate(rows):
        mine = [c for c in clashes if c.new_idx == i]
        blocking = sorted((c for c in mine if c.blocking), key=lambda c: c.existing.id)
        if blocking:
            first = blocking[0]
            reason = first.reason
            for extra in blocking[1:3]:
                reason += f" (also conflicts with #{extra.existing.id}: {extra.reason})"
            r.status, r.pending_reason = "pending", "conflict"
            r.conflicts_with, r.conflict_reason = first.existing.id, reason
            out.pending.append(r)
        else:
            for c in mine:
                out.overrides.append(Override(r, c.existing, c.reason))


async def add_rules(s: AsyncSession, llm, batch: RuleBatch, new: list[NewRule], *, origin: str,
                    item: PlanItem | None = None, source_message_id: int | None = None) -> AddOutcome:
    """Store new rules as active, after checking them against the rules they can apply with.

    * the check runs first on transient rows: if the LLM is busy the exception propagates and nothing is stored;
    * duplicates of an existing rule that already covers them are not stored (reported instead);
    * a blocking clash makes the NEW rule `pending` (reason + the clashing rule id), the existing rule stays active;
    * non-blocking clashes are returned in `overrides` (precedence settles them at generation time).
    """
    rows = [_build_row(batch, nr, origin, item, source_message_id) for nr in new]
    clashes, dups = await check_conflicts(llm, s, batch.course_id, rows) if rows else ([], [])
    out = AddOutcome()
    drop: set[int] = set()
    for i, ex in dups:
        if covers(ex, rows[i]):
            drop.add(i)
            out.duplicates.append((rows[i].rule_text, ex))
    kept_idx = [i for i in range(len(rows)) if i not in drop]
    kept = [rows[i] for i in kept_idx]
    remap = {old: new_i for new_i, old in enumerate(kept_idx)}
    kept_clashes = [Clash(remap[c.new_idx], c.existing, c.reason, c.blocking) for c in clashes if c.new_idx in remap]
    s.add_all(kept)
    _apply_clashes(kept, kept_clashes, out)
    await s.flush()
    out.rules = kept
    if not out.pending and not batch.resolved:
        batch.resolved = True
        out.resolved_batch = batch
    await s.commit()
    return out


async def activate_spec_rules(s: AsyncSession, llm, batch: RuleBatch, rules: list[FeedbackRule]) -> AddOutcome:
    """Approve worksheet-spec rules (pending/spec_review): check them like new rules; clashes become conflict cards,
    the rest become active. Raises (nothing changed) if the LLM is busy."""
    clashes, dups = await check_conflicts(llm, s, batch.course_id, rules) if rules else ([], [])
    out = AddOutcome(rules=list(rules))
    for i, ex in dups:
        out.duplicates.append((rules[i].rule_text, ex))  # reported only; the user approved these rules explicitly
    for r in rules:
        r.status, r.pending_reason = "active", None
    _apply_clashes(rules, clashes, out)
    await s.flush()
    if not out.pending and not batch.resolved:
        batch.resolved = True
        out.resolved_batch = batch
    await s.commit()
    return out


async def resolve_conflict(s: AsyncSession, rule_id: int, choice: str) -> RuleBatch | None:
    """The user's answer to a conflict card. `rule_id` is the NEW (pending) rule.

    old: new rule disabled. new: old rule disabled (replaced_by), new rule active. both: new rule active.
    Returns the batch iff this decision resolved it (caller then queues the regeneration, once).
    Returns None for an already-decided rule (double tap).
    """
    if choice not in ("old", "new", "both"):
        raise ValueError("choice must be old|new|both")
    r = await s.get(FeedbackRule, rule_id)
    if r is None or r.status != "pending" or r.pending_reason != "conflict":
        return None
    old = await s.get(FeedbackRule, r.conflicts_with) if r.conflicts_with else None
    if choice == "old":
        r.status = "disabled"
        r.disabled_reason = f"rejected: you kept #{old.id}" if old else "rejected by you"
    else:
        if choice == "new" and old is not None and old.status == "active":
            old.status, old.replaced_by = "disabled", r.id
            old.disabled_reason = f"replaced by #{r.id} (you chose Keep new)"
        r.status = "active"
    r.pending_reason = r.conflicts_with = r.conflict_reason = None
    await s.flush()
    batch = await _maybe_resolve(s, r.batch_id)
    await s.commit()
    return batch


async def _recheck(s, llm, r: FeedbackRule, probe: FeedbackRule) -> AddOutcome:
    """Re-run the conflict check for an existing rule using `probe` (a transient copy carrying the new text/scope)."""
    clashes, dups = await check_conflicts(llm, s, r.course_id, [probe], exclude_ids={r.id})
    out = AddOutcome(rules=[r])
    for _, ex in dups:
        out.duplicates.append((probe.rule_text, ex))  # reported, never deleted: this was an explicit user edit
    r.rule_text, r.scope, r.item_type, r.item_id = probe.rule_text, probe.scope, probe.item_type, probe.item_id
    r.status, r.pending_reason, r.conflicts_with, r.conflict_reason = "active", None, None, None
    _apply_clashes([r], [Clash(0, c.existing, c.reason, c.blocking) for c in clashes], out)
    await s.flush()
    out.resolved_batch = await _maybe_resolve(s, r.batch_id)
    await s.commit()
    return out


def _probe(r: FeedbackRule, **over) -> FeedbackRule:
    p = FeedbackRule(course_id=r.course_id, scope=over.get("scope", r.scope), item_type=over.get("item_type", r.item_type),
                     item_id=over.get("item_id", r.item_id), rule_text=over.get("text", r.rule_text))
    check_invariants(p)
    return p


async def edit_rule_text(s: AsyncSession, llm, rule_id: int, text: str) -> AddOutcome:
    """Explicit user edit. Active / conflict-pending rules are re-checked (a blocking clash makes the edited rule
    pending). Disabled and spec-review rules are only re-worded (restore / approval run the check)."""
    r = await s.get(FeedbackRule, rule_id)
    if r is None:
        raise KeyError(rule_id)
    text = text.strip()
    if not text:
        raise ValueError("rule text is empty")
    if r.status == "disabled" or (r.status == "pending" and r.pending_reason == "spec_review"):
        r.rule_text = text
        await s.commit()
        return AddOutcome(rules=[r])
    return await _recheck(s, llm, r, _probe(r, text=text))


async def change_scope(s: AsyncSession, llm, rule_id: int, scope: str, item_type: str | None = None,
                       item_id: int | None = None) -> AddOutcome:
    """Explicit user scope change (widen/narrow); re-runs the conflict check unless the rule is disabled/spec-review."""
    r = await s.get(FeedbackRule, rule_id)
    if r is None:
        raise KeyError(rule_id)
    probe = _probe(r, scope=scope, item_type=item_type if scope == "type" else None, item_id=item_id if scope == "item" else None)
    if r.status == "disabled" or (r.status == "pending" and r.pending_reason == "spec_review"):
        r.scope, r.item_type, r.item_id = probe.scope, probe.item_type, probe.item_id
        await s.commit()
        return AddOutcome(rules=[r])
    return await _recheck(s, llm, r, probe)


async def disable_rule(s: AsyncSession, rule_id: int, reason: str = "disabled by you") -> RuleBatch | None:
    """Explicit user action. Returns the batch if disabling a pending rule resolved it."""
    r = await s.get(FeedbackRule, rule_id)
    if r is None:
        return None
    r.status, r.disabled_reason = "disabled", reason
    r.pending_reason = r.conflicts_with = r.conflict_reason = None
    await s.flush()
    batch = await _maybe_resolve(s, r.batch_id)
    await s.commit()
    return batch


async def restore_rule(s: AsyncSession, llm, rule_id: int) -> AddOutcome:
    """disabled -> active, with a conflict check (a blocking clash makes it pending instead)."""
    r = await s.get(FeedbackRule, rule_id)
    if r is None:
        raise KeyError(rule_id)
    if r.status != "disabled":
        return AddOutcome(rules=[r])
    out = await _recheck(s, llm, r, _probe(r))
    r.disabled_reason = r.replaced_by = None
    await s.commit()
    return out


async def delete_rule_permanently(s: AsyncSession, rule_id: int) -> RuleBatch | None:
    """Only from an explicit confirm button. Active rules must be disabled first (a safety net)."""
    r = await s.get(FeedbackRule, rule_id)
    if r is None:
        return None
    if r.status == "active":
        raise ValueError("disable the rule before deleting it permanently")
    batch_id = r.batch_id
    await s.execute(update(FeedbackRule).where(FeedbackRule.conflicts_with == rule_id).values(conflicts_with=None))
    await s.execute(update(FeedbackRule).where(FeedbackRule.replaced_by == rule_id).values(replaced_by=None))
    await s.delete(r)
    await s.flush()
    batch = await _maybe_resolve(s, batch_id)
    await s.commit()
    return batch
