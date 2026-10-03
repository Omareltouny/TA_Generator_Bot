"""Scripted fake LLM provider + DB fixtures shared by acceptance tests."""
import re

from bot.db.models import Course, User
from bot.services import repo
from bot.services.llm_router import LLMRouter, RetryableError
from bot.services.outline_parser import validate_outline
from bot.services.planner import build_plan


class Scripted:
    """Behaves like a (dumb but format-correct) LLM. Echoes prompt rules into the output so tests can see them."""
    name, models = "fake", ["fake-1"]

    def __init__(self, n_questions=3, fail_titles=(), busy=False):
        self.n, self.fail_titles, self.busy, self.calls = n_questions, set(fail_titles), busy, []

    async def complete(self, model, system, prompt, json_mode, max_tokens=4096):
        self.calls.append(prompt)
        if self.busy:
            raise RetryableError("429", retry_after=30)
        if "extract structured data" in system:
            import json
            return json.dumps(MATH_OUTLINE)
        if "Convert the user's plan-edit" in system:
            import json
            return json.dumps({"ops": [{"op": "remove", "type": "tutorial", "seq": 12}]})
        if "turn a teaching assistant" in system:
            text = prompt.split("FEEDBACK:\n", 1)[1]
            rules = [{"text": text, "scope": "item" if "Q3 answer" in text else "course"}]
            return '{"rules": %s}' % __import__("json").dumps(rules)
        if "compare NEW rules" in system:
            old = re.findall(r"\[(\d+)\] (.*)", prompt.split("NEW RULES:")[0])
            new = re.findall(r"\[(\d+)\] (.*)", prompt.split("NEW RULES:")[1])
            sup = [{"new": int(ni), "old": int(oi)} for ni, nt in new for oi, ot in old
                   if ("Python" in nt and "Java" in ot) or ("Java" in nt and "Python" in ot)]
            return '{"supersedes": %s}' % __import__("json").dumps(sup)
        if "STUDENT VERSION" in prompt:
            title = re.search(r"Item: .* - (.*)", prompt).group(1)
            if any(t in title for t in self.fail_titles):
                return "garbage with no format"
            rules = re.search(r"<rules>(.*?)</rules>", prompt, re.S)
            qs = "\n\n".join(f"### Question {i}\nCompute $x_{i}^2$. {rules.group(1).strip() if rules else ''}" for i in range(1, self.n + 1))
            return f"=====TITLE=====\nGenerated {title}\n=====STUDENT=====\n{qs}"
        if "ANSWER KEY" in prompt:
            rules = re.search(r"<rules>(.*?)</rules>", prompt, re.S)
            return "\n\n".join(f"### Question {i}\nSolution: $x_{i}^2$ {rules.group(1).strip() if rules else ''}" for i in range(1, self.n + 1))
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
