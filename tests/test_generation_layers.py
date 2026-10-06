"""Prompt layering, heading word enforcement and style examples (rewrite spec 5, tests 8, 9, 11)."""
import pytest

from bot.db.models import Material, TypeFormat
from bot.services import generator, jobs, repo, rules
from bot.services.rules import NewRule
from tests.helpers import Scripted, make_llm, seed


@pytest.fixture
async def world(session):
    u, c, items = await seed(session)
    return session, u, c, {(i.type, i.seq): i for i in items}


async def add_rules(s, u, c, item, pairs):
    b = await rules.create_batch(s, course_id=c.id, user_id=u.id, kind="manual", chat_id=1)
    await rules.add_rules(s, make_llm(), b, [NewRule(t, sc) for t, sc in pairs], origin="manual", item=item)


async def first_student_prompt(s, prov, item):
    prov.calls.clear()
    await jobs.process_item(s, make_llm(prov), item)
    return next(p for p in prov.calls if "STUDENT VERSION" in p), next(p for p in prov.calls if "ANSWER KEY" in p)


async def test_prompt_layers(world):
    s, u, c, by = world
    lab = by[("lab", 1)]
    await add_rules(s, u, c, lab, [("Use Python", "course"), ("Labs have no starter code", "type"), ("Q3 answer is 42", "item")])
    student, key = await first_student_prompt(s, Scripted(), lab)
    for prompt in (student, key):
        i_tech, i_def, i_rules = (prompt.index(m) for m in ("TECHNICAL REQUIREMENTS", "DEFAULT STRUCTURE", "<rules>"))
        assert i_tech < i_def < i_rules
        r = prompt[i_rules:prompt.index("</rules>")]
        assert r.index("COURSE RULES") < r.index("RULES FOR ALL LABS") < r.index("RULES FOR THIS ITEM ONLY")
        assert r.rindex("Q3 answer is 42") > r.rindex("Labs have no starter code") > r.rindex("Use Python")  # item rule last
    assert "TA RULES always win" in student
    # the rules reached the item's version snapshot
    v = await repo.get_version(s, lab.id)
    assert len(v.feedback_rule_ids) == 3


async def test_empty_rule_sections_are_omitted(world):
    s, u, c, by = world
    student, _ = await first_student_prompt(s, Scripted(), by[("lab", 1)])
    assert "<rules>" not in student and "TECHNICAL REQUIREMENTS" in student and "DEFAULT STRUCTURE" in student


async def test_item_rule_does_not_leak_and_type_rule_is_type_scoped(world):
    s, u, c, by = world
    await add_rules(s, u, c, by[("lab", 1)], [("Q3 answer is 42", "item"), ("Labs use pseudo code", "type")])
    lab2, _ = await first_student_prompt(s, Scripted(), by[("lab", 2)])
    asg, _ = await first_student_prompt(s, Scripted(), by[("assignment", 1)])
    assert "Q3 answer is 42" not in lab2 and "Labs use pseudo code" in lab2
    assert "Q3 answer is 42" not in asg and "Labs use pseudo code" not in asg


def test_heading_word_enforced():
    exercise = "### Exercise 1\nA\n\n### Exercise 2\nB"
    question = "### Question 1\nA\n\n### Question 2\nB"
    generator.validate_student(exercise, "Exercise")            # lab with Exercise headings passes
    with pytest.raises(ValueError, match="Exercise"):
        generator.validate_student(question, "Exercise")        # ... Question fails when the format is Exercise
    with pytest.raises(ValueError):
        generator.validate_student(exercise + "\n\n### Question 3\nC", "Exercise")   # mixed words fail
    generator.validate_student(question)                        # no word configured: any accepted word is fine
    generator.validate_key(exercise, exercise, "Exercise")
    with pytest.raises(ValueError):
        generator.validate_key(exercise, question, "Exercise")


async def test_heading_word_comes_from_type_format(world):
    s, u, c, by = world
    lab = by[("lab", 1)]
    assert await repo.get_heading_word(s, c.id, "lab") == "Task" and await repo.get_heading_word(s, c.id, "assignment") == "Question"
    s.add(TypeFormat(course_id=c.id, item_type="lab", heading_word="Exercise"))
    await s.commit()
    prov = Scripted()
    with pytest.raises(generator.GenerationError, match="Exercise"):
        await jobs.process_item(s, make_llm(prov), lab)           # fake emits Task headings -> rejected twice
    # the retry note names the required word
    assert any("Exercise" in p for p in prov.calls)
    prov2 = Scripted(heading="Exercise")
    await jobs.process_item(s, make_llm(prov2), lab)               # compliant output passes
    assert "### Exercise N" in next(p for p in prov2.calls if "STUDENT VERSION" in p)


async def test_examples_in_prompt(world):
    s, u, c, by = world
    for i in range(3):
        s.add(Material(course_id=c.id, kind="reference", filename=f"lab{i}.pdf", extracted_text=f"EXAMPLE{i} " + "x" * 6000,
                       uploaded_by=u.id, item_type="lab", is_example=True))
    s.add(Material(course_id=c.id, kind="reference", filename="tut.pdf", extracted_text="TUTEXAMPLE", uploaded_by=u.id,
                   item_type="tutorial", is_example=True))
    await s.commit()
    prov = Scripted()
    prov.calls.clear()
    await jobs.process_item(s, make_llm(prov), by[("lab", 1)], examples_per_type=2, example_max_chars=3500)
    p = next(x for x in prov.calls if "STUDENT VERSION" in x)
    assert p.count("<style_example") == 2 and "EXAMPLE0" in p and "EXAMPLE1" in p and "EXAMPLE2" not in p
    assert "TUTEXAMPLE" not in p
    block = p[p.index("<style_example"):p.index("</style_example>")]
    assert len(block.split(">", 1)[1].strip()) == 3500                       # truncated to EXAMPLE_MAX_CHARS
    assert "Do NOT reuse their questions" in p and "TA RULES override the examples" in p
    assert p.index("</rules>") < p.index("<style_example") if "<rules>" in p else True
    assert p.index("<style_example") < p.index("Item: LAB")                  # examples come before the context
