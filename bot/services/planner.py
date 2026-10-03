"""Outline -> plan items (spec 6.3, with owner rules: one lab/tutorial per week, ~3 assignments), plus plan-edit operations. Pure functions; no DB/Telegram/LLM
except `edit_ops_from_text`, which takes an injected router."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Optional

from bot.services.outline_parser import Outline

ASSIGNMENT_RE = re.compile(r"\b(assign\w*|homework|hw\s*\d*|ass\.?\s*\d+|problem\s*set|pset|project)\b", re.I)
NON_ASSIGNMENT_RE = re.compile(r"\b(quiz\w*|exam\w*|midterm\w*|final|participation|attendance|activit\w+)\b", re.I)
# A bare term project ("Project", "Final Project") is a term deliverable, not a handout: not auto-planned.
TERM_PROJECT_RE = re.compile(r"^\s*((final|term|course|group|capstone|team|major)\s+)?project\s*\d*\s*$", re.I)
# A schedule entry that is only an assessment/break (as opposed to teaching content).
SKIP_WEEK_RE = re.compile(r"\b(mid-?terms?|quiz\w*|exams?|study|review|revision|reading\s+week|break|holiday|"
                          r"project\s+(delivery|presentations?|demo)|final\s+project|presentations?|no\s+(class|lecture|lab))\b", re.I)
TUTORIAL_RE = re.compile(r"\b(tutorials?|worksheets?|problem\s*(set|solving))\b", re.I)
LAB_RE = re.compile(r"\blab\w*\b", re.I)
EMPTY_TEXT = {"", "n/a", "na", "none", "-", "—", "tba", "optional"}
GENERIC_LONG_RE = re.compile(r"(further\s+)?exercises?|related topic|participation", re.I)


@dataclass
class Draft:
    type: str                      # lab | tutorial | assignment
    title: str
    week: Optional[int] = None
    topics: list[str] = field(default_factory=list)
    due_date: Optional[str] = None
    weight: Optional[str] = None
    source: str = "outline"        # outline | inferred | user
    seq: int = 0
    id: Optional[int] = None       # plan_items.id once persisted


def _week_from_due(due: str | None) -> Optional[int]:
    m = re.search(r"week\s*(\d+)", due or "", re.I)
    return int(m.group(1)) if m else None


def _clean_topics(topics: list[str]) -> list[str]:
    """Drop assessment mentions inside a lecture entry (e.g. 'Quiz 1 - in the beginning')."""
    return [t for t in topics if not SKIP_WEEK_RE.search(t)]


def _merge_weeks(rows) -> list[dict]:
    """One entry per week. Several sessions in a week (e.g. Mon + Wed rows) are merged; blank merged-cell weeks
    inherit the previous topics; a week whose only content is an exam/quiz/review/project is marked skip."""
    by: dict[int, dict] = {}
    for r in sorted((r for r in rows if r.week is not None), key=lambda r: r.week):
        w = by.setdefault(r.week, {"week": r.week, "topics": [], "lab": "", "skip_hits": 0, "rows": 0})
        w["rows"] += 1
        kept = _clean_topics(r.topics)
        w["topics"] += [t for t in kept if t not in w["topics"]]
        if r.topics and not kept:
            w["skip_hits"] += 1
        if (r.notes and SKIP_WEEK_RE.search(r.notes)) and not kept:
            w["skip_hits"] += 1
        lab = (r.lab_or_tutorial_text or "").strip()
        if lab.lower() not in EMPTY_TEXT and not w["lab"]:
            w["lab"] = lab
    out, prev = [], []
    for w in by.values():
        w["skip"] = w["skip_hits"] > 0 and not w["topics"]
        if w["skip"]:
            prev = []
        else:
            if w["topics"]:
                prev = list(w["topics"])
            else:
                w["topics"] = list(prev)  # merged-cell week: inherit
        out.append(w)
    return out


def _title(kind: str, text: str, topics: list[str]) -> str:
    t0 = topics[0] if topics else ""
    if len(text) <= 24:
        return f"{text}: {t0}"[:120] if t0 and not t0.lower().startswith(text.lower()) else text
    if GENERIC_LONG_RE.search(text):
        return f"{kind.title()}: {t0}"[:120] if t0 else text[:120]
    return text[:120]


def build_plan(o: Outline, default_assignments: int = 3) -> list[Draft]:
    """One lab/tutorial per teaching week; assignments as stated, else 3 (inferred)."""
    drafts: list[Draft] = []

    for a in o.assessments:
        if (ASSIGNMENT_RE.search(a.name) and not NON_ASSIGNMENT_RE.search(a.name)
                and not TERM_PROJECT_RE.search(a.name)):
            drafts.append(Draft("assignment", a.name.strip(), a.week or _week_from_due(a.due), [], a.due, a.weight))

    weeks = [w for w in _merge_weeks(o.schedule) if not w["skip"] and (w["topics"] or w["lab"])]
    texts = [w["lab"] for w in weeks if w["lab"]]
    # Lab by default for software/programming courses, tutorial otherwise (math, theory).
    default_kind = "lab" if (o.software or o.language_hint) else "tutorial"
    column_kind = None
    if texts:
        column_kind = "lab" if any(LAB_RE.search(t) for t in texts) and not any(TUTORIAL_RE.search(t) for t in texts) else None
    for w in weeks:
        text, topics = w["lab"], w["topics"]
        if text:
            kind = "lab" if (LAB_RE.search(text) and not TUTORIAL_RE.search(text)) or (column_kind == "lab") else \
                   ("tutorial" if TUTORIAL_RE.search(text) else default_kind)
            drafts.append(Draft(kind, _title(kind, text, topics), w["week"], topics))
        else:
            kind = default_kind if not texts else (drafts[-1].type if drafts and drafts[-1].type != "assignment" else default_kind)
            drafts.append(Draft(kind, f"{kind.title()}: {topics[0]}"[:120], w["week"], topics, source="inferred"))

    if not any(d.type == "assignment" for d in drafts):
        wk = [w["week"] for w in weeks] or [None]
        for i in range(default_assignments):
            w = wk[max(0, min(len(wk) - 1, round((i + 1) * len(wk) / (default_assignments + 1)) - 1))]
            drafts.append(Draft("assignment", f"Assignment {i + 1}", w, source="inferred"))

    return _renumber(drafts)


ORDER = {"lab": 0, "tutorial": 1, "assignment": 2}


def _renumber(drafts: list[Draft]) -> list[Draft]:
    drafts.sort(key=lambda d: (ORDER[d.type], d.week if d.week is not None else 999, d.seq))
    counts: dict[str, int] = {}
    for d in drafts:
        counts[d.type] = counts.get(d.type, 0) + 1
        d.seq = counts[d.type]
    return drafts


# ---- plan edit operations ---------------------------------------------------

@dataclass
class Op:
    op: str                         # add | remove | move | rename
    type: Optional[str] = None
    seq: Optional[int] = None       # target item (1-based within type)
    week: Optional[int] = None
    title: Optional[str] = None
    topics: list[str] = field(default_factory=list)


def apply_ops(plan: list[Draft], ops: list[Op]) -> tuple[list[Draft], list[str]]:
    """Returns (new plan, human-readable log incl. ops that could not be applied)."""
    plan, log = [replace(d) for d in plan], []

    def find(op: Op) -> Optional[Draft]:
        return next((d for d in plan if d.type == op.type and d.seq == op.seq), None)

    # Resolve targets against the numbering the user saw, before renumbering mutates it.
    resolved = [(op, find(op) if op.op in ("remove", "move", "rename") else None) for op in ops]
    for op, target in resolved:
        if op.op == "add" and op.type in ORDER and op.title:
            plan.append(Draft(op.type, op.title, op.week, op.topics, source="user"))
            log.append(f"Added {op.type}: {op.title}")
        elif op.op in ("remove", "move", "rename"):
            if target is None:
                log.append(f"Could not find {op.type} {op.seq} ({op.op})")
            elif op.op == "remove":
                plan.remove(target)
                log.append(f"Removed {target.type} {target.seq}: {target.title}")
            elif op.op == "move":
                target.week = op.week
                log.append(f"Moved {target.type} {target.seq} to week {op.week}")
            else:
                target.title = op.title or target.title
                log.append(f"Renamed {target.type} {target.seq} to {target.title}")
        else:
            log.append(f"Ignored invalid operation: {op.op}")
    return _renumber(plan), log


def format_plan(plan: list[Draft]) -> list[str]:
    """Plan as message chunks (<4096 chars), grouped by type."""
    chunks, cur = [], ""
    for t in ORDER:
        items = [d for d in plan if d.type == t]
        if not items:
            continue
        block = f"{t.upper()}S\n" + "".join(
            f"{d.seq}. {'W' + str(d.week) if d.week else 'W?'} - {d.title}"
            + (f" [due {d.due_date}]" if d.due_date else "") + (f" ({d.weight})" if d.weight else "")
            + (f"\n   topics: {', '.join(d.topics)[:200]}" if d.topics else "")
            + f"  [{d.source}]\n" for d in items) + "\n"
        if len(cur) + len(block) > 3800 and cur:
            chunks.append(cur)
            cur = ""
        while len(block) > 3800:  # a single huge group: split by lines
            cut = block.rfind("\n", 0, 3800)
            chunks.append(block[:cut])
            block = block[cut + 1:]
        cur += block
    if cur:
        chunks.append(cur)
    return chunks or ["(plan is empty)"]


EDIT_SYSTEM = ("Convert the user's plan-edit request into operations. Items are referenced as (type, seq) using the "
               "numbering shown. Return JSON: {\"ops\": [{\"op\": \"add|remove|move|rename\", \"type\": \"lab|tutorial|assignment\", "
               "\"seq\": int|null, \"week\": int|null, \"title\": str|null, \"topics\": [str]}]}. Do not invent operations.")


async def edit_ops_from_text(llm, plan: list[Draft], text: str) -> list[Op]:
    prompt = "CURRENT PLAN:\n" + "\n".join(format_plan(plan)) + f"\nUSER REQUEST:\n{text}"

    def v(d):
        if not isinstance(d.get("ops"), list):
            raise ValueError("ops must be a list")
    data, _ = await llm.complete_json(prompt, system=EDIT_SYSTEM, validate=v)
    return [Op(op=str(o.get("op")), type=o.get("type"), seq=o.get("seq"), week=o.get("week"),
               title=o.get("title"), topics=o.get("topics") or []) for o in data["ops"]]
