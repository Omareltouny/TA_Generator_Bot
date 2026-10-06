"""Plain-text cards for rules: result card, conflict cards, pending-decisions page (rewrite spec 6.1).

Cards are pure functions of DB state, so they can be re-rendered after every button tap and survive restarts.
Callback data (all <= 64 bytes): cf:<rule_id>:<old|new|both|edit|back>, sc:<rule_id>:<scope>, un:<rule_id>.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import FeedbackRule, PlanItem, RuleBatch
from bot.handlers import ui
from bot.services import rules
from bot.services.rules import AddOutcome

TYPE_PLURAL = {"lab": "labs", "tutorial": "tutorials", "assignment": "assignments"}


def tag(r: FeedbackRule) -> str:
    return "course" if r.scope == "course" else (f"all {TYPE_PLURAL.get(r.item_type, r.item_type)}" if r.scope == "type" else "this item")


def rule_line(r: FeedbackRule) -> str:
    return f"#{r.id} [{tag(r)}] {r.rule_text}"


def _short(text: str, n: int = 300) -> str:
    return text if len(text) <= n else text[:n - 1] + "..."


def conflict_block(new: FeedbackRule, old: FeedbackRule | None) -> str:
    old_id = f"#{new.conflicts_with}" if new.conflicts_with else "an earlier rule"
    lines = [f"#{new.id} conflicts with {old_id}. Reason: {new.conflict_reason or '(none given)'}",
             f'  New #{new.id}: "{_short(new.rule_text)}"']
    if old is not None:
        gone = "" if old.status == "active" else f" ({old.status} now)"
        lines.append(f'  Existing #{old.id}: "{_short(old.rule_text)}"{gone}')
    return "\n".join(lines)


def scope_row(r: FeedbackRule, item_type: str | None) -> list[tuple[str, str]]:
    """[#41: all labs] [#41: whole course] [Undo #41] depending on what widening is possible."""
    row: list[tuple[str, str]] = []
    if r.scope == "item" and item_type:
        row.append((f"#{r.id}: all {TYPE_PLURAL.get(item_type, item_type)}", f"sc:{r.id}:type"))
    if r.scope != "course":
        row.append((f"#{r.id}: whole course", f"sc:{r.id}:course"))
    row.append((f"Undo #{r.id}", f"un:{r.id}"))
    return row


def conflict_row(new: FeedbackRule) -> list[tuple[str, str]]:
    return [(f"Keep #{new.conflicts_with}", f"cf:{new.id}:old"), (f"Keep #{new.id}", f"cf:{new.id}:new"),
            ("Both are fine", f"cf:{new.id}:both"), (f"Edit #{new.id}", f"cf:{new.id}:edit")]


async def _rules_by_id(s: AsyncSession, ids) -> dict[int, FeedbackRule]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {r.id: r for r in (await s.execute(select(FeedbackRule).where(FeedbackRule.id.in_(ids)))).scalars()}


async def regen_line(s: AsyncSession, batch: RuleBatch) -> str:
    item = await s.get(PlanItem, batch.regen_item_id) if batch.regen_item_id else None
    if item is None:
        return ""
    parts = await rules.in_use_summary(s, item.course_id, [item])
    return f"Regenerating {item.type.title()} {item.seq} with all rules ({parts.removeprefix('Rules in use: ')})."


async def render_batch_card(s: AsyncSession, batch: RuleBatch, extra: list[str] | None = None, *, with_regen: bool = True):
    """(text, markup) for the card of one rule batch, from current DB state."""
    rows = list((await s.execute(select(FeedbackRule).where(FeedbackRule.batch_id == batch.id).order_by(FeedbackRule.id))).scalars())
    pending = [r for r in rows if r.status == "pending" and r.pending_reason == "conflict"]
    kept = [r for r in rows if r.status == "active"]
    gone = [r for r in rows if r.status == "disabled"]
    olds = await _rules_by_id(s, [r.conflicts_with for r in pending])
    types = dict((await s.execute(select(PlanItem.id, PlanItem.type).where(
        PlanItem.id.in_({r.item_id for r in rows if r.item_id})))).all()) if any(r.item_id for r in rows) else {}
    lines: list[str] = []
    if pending and batch.regen_item_id:
        lines.append("Regeneration starts after you decide.")
    if kept:
        suffix = {"feedback": " from your feedback", "spec": " from your worksheets"}.get(batch.kind, "")
        lines.append(f"Saved {len(kept)} rule{'s' if len(kept) != 1 else ''}{suffix}:")
        lines += [rule_line(r) for r in kept]
        for r in kept:
            for old in (await s.execute(select(FeedbackRule).where(FeedbackRule.replaced_by == r.id))).scalars():
                lines.append(f"  #{r.id} replaced #{old.id} (you chose Keep new): \"{_short(old.rule_text, 120)}\"")
    for r in pending:
        lines.append(conflict_block(r, olds.get(r.conflicts_with)))
    for r in gone:
        lines.append(f"#{r.id} [{tag(r)}] {_short(r.rule_text, 160)}\n  Not kept ({r.disabled_reason or 'disabled'}). Restore it any time in /rules > Disabled.")
    lines += extra or []
    if with_regen and not pending and batch.regen_item_id and batch.resolved:
        rl = await regen_line(s, batch)
        if rl:
            lines.append(rl)
    if not lines:
        lines.append("Nothing to save.")
    kb_rows = [conflict_row(r) for r in pending]
    if batch.kind != "spec":
        kb_rows += [scope_row(r, types.get(r.item_id) if r.scope == "item" else None) for r in kept]
    return "\n".join(lines), (ui.kb(*kb_rows) if kb_rows else None)


def outcome_notes(out: AddOutcome) -> list[str]:
    """Informational lines: non-blocking overrides and duplicates that were not stored."""
    notes = []
    for o in out.overrides:
        where = "this item" if o.rule.scope == "item" else tag(o.rule)
        notes.append(f"Note: for {where}, #{o.rule.id} overrides {tag(o.existing)} rule #{o.existing.id} (reason: {o.reason})")
    for text, ex in out.duplicates:
        notes.append(f'Already covered by #{ex.id}: "{_short(ex.rule_text, 120)}"')
    return notes


async def render_pending_page(s: AsyncSession, course_id: int):
    """(text, markup) listing every undecided conflict of the course, plus worksheet specs awaiting review."""
    conflicts = await rules.pending_rules(s, course_id, "conflict")
    specs = await rules.pending_rules(s, course_id, "spec_review")
    olds = await _rules_by_id(s, [r.conflicts_with for r in conflicts])
    lines = [f"Pending decisions ({len(conflicts) + (1 if specs else 0)})", ""]
    for r in conflicts:
        lines += [conflict_block(r, olds.get(r.conflicts_with)), ""]
    kb_rows = [conflict_row(r) for r in conflicts]
    if specs:
        batches = sorted({r.batch_id for r in specs if r.batch_id})
        lines.append(f"{len(specs)} worksheet-format rule(s) wait for your review.")
        kb_rows += [[("Review worksheet format", f"ws:rev:{b}")] for b in batches]
    if not conflicts and not specs:
        lines = ["Nothing is waiting for your decision."]
    return "\n".join(lines).strip(), (ui.kb(*kb_rows) if kb_rows else None)
