"""Outline text -> validated outline_json (spec 7.1). Never invents values: unknown => null."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, ValidationError, field_validator


class ScheduleRow(BaseModel):
    week: Optional[int] = None
    topics: list[str] = Field(default_factory=list)
    lab_or_tutorial_text: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("week", mode="before")
    @classmethod
    def _week(cls, v):
        if v in (None, ""):
            return None
        try:
            return int(str(v).strip().split()[-1].split("-")[0])
        except ValueError:
            return None


class Assessment(BaseModel):
    name: str
    type: Optional[str] = None
    weight: Optional[str] = None
    due: Optional[str] = None
    week: Optional[int] = None

    @field_validator("weight", "due", mode="before")
    @classmethod
    def _str(cls, v):
        return None if v in (None, "") else str(v)

    @field_validator("week", mode="before")
    @classmethod
    def _week(cls, v):
        try:
            return None if v in (None, "") else int(v)
        except (ValueError, TypeError):
            return None


class Outline(BaseModel):
    course_code: Optional[str] = None
    course_name: Optional[str] = None
    term: Optional[str] = None
    language_hint: Optional[str] = None
    description: Optional[str] = None
    textbooks: list[str] = Field(default_factory=list)
    software: list[str] = Field(default_factory=list)
    learning_objectives: list[str] = Field(default_factory=list)
    schedule: list[ScheduleRow] = Field(default_factory=list)
    assessments: list[Assessment] = Field(default_factory=list)

    @field_validator("textbooks", "software", "learning_objectives", "schedule", "assessments", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return [] if v is None else v


SYSTEM = ("You extract structured data from university course outlines. Use ONLY information present in the text. "
          "If a field is not stated, use null (or [] for lists). Never invent or infer values. "
          "A schedule row = one week; if a week has several sessions (e.g. Monday and Wednesday rows), put all their topics in that one week. "
          "Put text from a lab/tutorial/worksheet/exercises column in lab_or_tutorial_text, nothing else. "
          "If one grading row groups numbered items (e.g. 'Assignments 20%: Ass 1 week 2, Ass 2 week 5, Ass 3 week 6'), "
          "emit one assessment per item (split the weight evenly only if the outline gives a total and no per-item weights, otherwise null). "
          "Keep quizzes, midterms, exams and projects as separate assessments. "
          "Return a single JSON object and nothing else.")

SCHEMA_HINT = """{
 "course_code": str|null, "course_name": str|null, "term": str|null,
 "language_hint": str|null  (programming language/software stack ONLY if the outline states it),
 "description": str|null, "textbooks": [str], "software": [str], "learning_objectives": [str],
 "schedule": [{"week": int|null, "topics": [str], "lab_or_tutorial_text": str|null, "notes": str|null}],
 "assessments": [{"name": str, "type": str|null, "weight": str|null, "due": str|null, "week": int|null}]
}"""

MAX_OUTLINE_CHARS = 60_000


def validate_outline(data: dict) -> Outline:
    try:
        return Outline.model_validate(data)
    except ValidationError as e:
        raise ValueError(str(e)) from e


def build_prompt(outline_text: str, corrections: str | None = None) -> str:
    p = f"Extract this outline into JSON matching:\n{SCHEMA_HINT}\n\nOUTLINE TEXT:\n{outline_text[:MAX_OUTLINE_CHARS]}"
    if corrections:
        p += f"\n\nThe user corrected the previous parse; apply these corrections:\n{corrections}"
    return p


async def parse_outline(llm, outline_text: str) -> tuple[Outline, object]:
    data, res = await llm.complete_json(build_prompt(outline_text), system=SYSTEM,
                                        validate=lambda d: validate_outline(d), max_tokens=6000)
    return validate_outline(data), res


async def apply_corrections(llm, current: Outline, corrections: str) -> Outline:
    prompt = (f"Current parsed outline JSON:\n{current.model_dump_json()}\n\nUser corrections (free text):\n{corrections}\n\n"
              f"Return the full updated JSON in this schema:\n{SCHEMA_HINT}")
    data, _ = await llm.complete_json(prompt, system=SYSTEM, validate=lambda d: validate_outline(d), max_tokens=6000)
    return validate_outline(data)


def summarize(o: Outline) -> str:
    weeks = len({r.week for r in o.schedule if r.week is not None}) or len(o.schedule)
    ass = ", ".join(f"{a.name}{f' ({a.weight})' if a.weight else ''}" for a in o.assessments) or "none found"
    return (f"{o.course_code or '?'} - {o.course_name or '(name not found)'}\n"
            f"Term: {o.term or 'not stated'} | Stack: {o.language_hint or 'not stated'}\n"
            f"Schedule rows: {weeks} | Assessments: {ass}\n\n"
            "Reply with any corrections in plain text, or tap Continue.")
