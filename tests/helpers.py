"""Scripted fake LLM provider + DB fixtures shared by acceptance tests."""
import re

from bot.db.models import Course, User
from bot.services import repo
from bot.services.llm_router import LLMRouter, RetryableError
from bot.services.outline_parser import validate_outline
from bot.services.planner import build_plan


def default_scope(text: str) -> str:
    """Deterministic stand-in for the LLM's scope guess (default is the narrow one, like the real prompt)."""
    t = text.lower()
    if "all labs" in t or "every lab" in t:
        return "type"
    if "everything" in t or "everywhere" in t or "always" in t or t.startswith("use python") or t.startswith("use java"):
        return "course"
    return "item"


def default_conflict(new_text: str, old_text: str):
    """Reason string if the fake considers the pair contradictory, else None."""
    if ("Python" in new_text and "Java" in old_text) or ("Java" in new_text and "Python" in old_text):
        return "One rule requires Python while the other requires Java."
    return None


class Scripted:
    """Behaves like a (dumb but format-correct) LLM. Echoes prompt rules into the output so tests can see them.

    Knobs: `conflict_fn(new_text, old_text) -> reason|None` (e.g. to emulate false positives),
    `spec_heading` / `spec_rules` for the worksheet format-spec call, `n` questions, `heading` word used in output.
    """
    name, models = "fake", ["fake-1"]

    def __init__(self, n_questions=3, fail_titles=(), busy=False, heading=None):
        self.n, self.fail_titles, self.busy, self.calls = n_questions, set(fail_titles), busy, []
        self.heading = heading            # None -> Task for labs, Question otherwise (matches the default format)
        self.conflict_fn = default_conflict
        self.spec_heading = "Question"
        self.spec_rules = [{"text": "Start every worksheet with a short objectives list.", "aspect": "structure"},
                           {"text": "Show the marks for each question in brackets.", "aspect": "marks"}]
        self.system_calls = []

    async def complete(self, model, system, prompt, json_mode, max_tokens=4096):
        import json
        self.calls.append(prompt)
        self.system_calls.append(system)
        if self.busy:
            raise RetryableError("429", retry_after=30)
        if "extract structured data" in system:
            return json.dumps(MATH_OUTLINE)
        if "Convert the user's plan-edit" in system:
            return json.dumps({"ops": [{"op": "remove", "type": "tutorial", "seq": 12}]})
        if "convert a teaching assistant" in system:
            text = prompt.split("FEEDBACK:\n", 1)[1]
            fixed = re.search(r"^SCOPE: (\w+)$", prompt, re.M)
            rules = [{"text": ln.strip(), "scope": fixed.group(1) if fixed else default_scope(ln)}
                     for ln in text.splitlines() if ln.strip()]
            return json.dumps({"rules": rules})
        if "check whether NEW rules" in system:
            old = re.findall(r"^\[(\d+)\] \([^)]*\) (.*)$", prompt.split("NEW RULES:")[0], re.M)
            new = re.findall(r"^\[(\d+)\] \([^)]*\) (.*)$", prompt.split("NEW RULES:")[1], re.M)
            conflicts, dups = [], []
            for ni, nt in new:
                for oi, ot in old:
                    if nt.strip().lower() == ot.strip().lower():
                        dups.append({"new": int(ni), "existing": int(oi)})
                        continue
                    reason = self.conflict_fn(nt, ot)
                    if reason is not None:
                        conflicts.append({"new": int(ni), "existing": int(oi), "reason": reason})
            return json.dumps({"conflicts": conflicts, "duplicates": dups})
        if "describe ONLY their format" in system:
            return json.dumps({"heading_word": self.spec_heading, "rules": self.spec_rules})
        hw = self.heading or ("Task" if re.search(r"Item: LAB", prompt) else "Question")
        if "STUDENT VERSION" in prompt:
            title = re.search(r"Item: .* - (.*)", prompt).group(1)
            if any(t in title for t in self.fail_titles):
                return "garbage with no format"
            rules = re.search(r"<rules>(.*?)</rules>", prompt, re.S)
            qs = "\n\n".join(f"### {hw} {i}\nCompute $x_{i}^2$. {rules.group(1).strip() if rules else ''}" for i in range(1, self.n + 1))
            return f"=====TITLE=====\nGenerated {title}\n=====STUDENT=====\n{qs}"
        if "ANSWER KEY" in prompt:
            rules = re.search(r"<rules>(.*?)</rules>", prompt, re.S)
            return "\n\n".join(f"### {hw} {i}\nSolution: $x_{i}^2$ {rules.group(1).strip() if rules else ''}" for i in range(1, self.n + 1))
        raise AssertionError("unexpected prompt")


def make_llm(provider=None):
    async def nosleep(_): pass
    return LLMRouter([provider or Scripted()], sleep=nosleep)


OUTLINE = {"course_code": "CS 1000", "course_name": "Intro Test", "software": ["Java"],
           "schedule": [{"week": w, "topics": [f"Topic {w}"], "lab_or_tutorial_text": f"Lab {w}"} for w in (1, 2, 3)],
           "assessments": [{"name": f"Assignment {i}", "weight": "10%", "due": f"Week {i * 3}"} for i in (1, 2, 3)]}


async def seed(session):
    u = User(telegram_id=111, name="ta")
    session.add(u)
    await session.flush()
    c = Course(created_by_user_id=u.id, outline_json=validate_outline(OUTLINE).model_dump(), code="CS 1000",
               name="Intro Test", plan_confirmed=True)
    session.add(c)
    await session.flush()
    items = await repo.save_plan(session, c.id, build_plan(validate_outline(OUTLINE)))
    return u, c, items


TOPICS = ["Substitution", "Parts", "Trig Integrals", "Trig Subs", "Partial Fractions", "Applications", "Improper",
          "Sequences", "Divergence Test", "Comparison", "Absolute Convergence", "Power Series"]
MATH_OUTLINE = {"course_code": "MATH 1920", "course_name": "Single Variable Calculus II", "term": "Fall 2025",
                "schedule": [{"week": i + 1, "topics": [t], "lab_or_tutorial_text": f"Worksheet {i + 1}"} for i, t in enumerate(TOPICS)],
                "assessments": [{"name": f"Assignment {i}", "weight": "10%", "due": f"{i} Dec 2025"} for i in (1, 2, 3)]
                + [{"name": "Quiz 1", "weight": "5%"}, {"name": "Final Exam", "weight": "30%"}]}


async def feedback_round(s, llm, *, course_id, item_id, user_id, texts, item_desc="item"):
    """Service-level equivalent of the feedback handler (no Telegram): extract -> add_rules inside a batch.
    Returns the AddOutcome with `.batch` attached."""
    from bot.db.models import FeedbackMessage, PlanItem
    from bot.services import rules
    raw = "\n".join(t.strip() for t in texts if t.strip())
    item = await s.get(PlanItem, item_id)
    fm = FeedbackMessage(course_id=course_id, item_id=item_id, user_id=user_id, raw_text=raw)
    s.add(fm)
    await s.flush()
    new = await rules.extract_rules(llm, raw, item_desc, item.type)
    batch = await rules.create_batch(s, course_id=course_id, user_id=user_id, kind="feedback", chat_id=1, regen_item_id=item_id)
    out = await rules.add_rules(s, llm, batch, new, origin="feedback", item=item, source_message_id=fm.id)
    out.batch = batch
    return out


async def active_rule_texts(s, course_id, item):
    from bot.services import rules
    a = await rules.applicable_rules(s, course_id, item)
    return [r.rule_text for r in a.course], [r.rule_text for r in a.type], [r.rule_text for r in a.item]
