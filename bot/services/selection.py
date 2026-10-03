"""Free-text item selection: "labs 2-5 and assignment 1", "all tutorials", "everything"."""
from __future__ import annotations

import re

TYPES = {"lab": "lab", "labs": "lab", "tutorial": "tutorial", "tutorials": "tutorial",
         "assignment": "assignment", "assignments": "assignment", "hw": "assignment", "homework": "assignment"}
_NUM = r"\d+\s*(?:-|–|to)\s*\d+|\d+"
_RE = re.compile(rf"\b(labs?|tutorials?|assignments?|homework|hw)\b\s*((?:{_NUM})(?:\s*(?:,|and|&)\s*(?:{_NUM}))*)?", re.I)


def _nums(spec: str) -> list[int]:
    out = []
    for part in re.split(r"\s*(?:,|and|&)\s*", spec.strip()):
        m = re.fullmatch(r"(\d+)\s*(?:-|–|to)\s*(\d+)", part)
        out += list(range(int(m[1]), int(m[2]) + 1)) if m else ([int(part)] if part.isdigit() else [])
    return out


def parse_selection(text: str, items: list) -> tuple[list, list[str]]:
    """items need .type and .seq. Returns (selected, problems)."""
    t = text.lower().strip()
    if re.search(r"\b(everything|all items)\b", t):
        return list(items), []
    sel, problems = [], []
    for m in _RE.finditer(t):
        typ = TYPES[m[1].lower()]
        pool = [i for i in items if i.type == typ]
        seqs = _nums(m[2]) if m[2] else [i.seq for i in pool]
        for sq in seqs:
            hit = next((i for i in pool if i.seq == sq), None)
            if hit is None:
                problems.append(f"no {typ} {sq}")
            elif hit not in sel:
                sel.append(hit)
    if not sel and not problems:
        problems.append("couldn't understand the selection")
    return sel, problems
