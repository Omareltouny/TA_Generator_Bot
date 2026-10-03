import pytest

from bot.services.llm_router import LLMRouter
from bot.services.outline_parser import Outline, parse_outline, summarize, validate_outline
from tests.test_llm_router import Stub, nosleep


def test_missing_fields_stay_null():
    o = validate_outline({"course_code": "MATH 1920", "schedule": None, "assessments": [{"name": "HW1", "weight": 10, "week": "x"}]})
    assert o.course_name is None and o.schedule == [] and o.language_hint is None
    assert o.assessments[0].weight == "10" and o.assessments[0].week is None


def test_week_coercion():
    o = validate_outline({"schedule": [{"week": "Week 3"}, {"week": "4-5"}, {"week": ""}]})
    assert [r.week for r in o.schedule] == [3, 4, None]


def test_invalid_raises_valueerror():
    with pytest.raises(ValueError):
        validate_outline({"assessments": [{"weight": "10"}]})  # name required


async def test_parse_retries_on_invalid():
    stub = Stub("a", ["m"], ['{"assessments": [{"weight": "1"}]}', '{"course_code": "CS-1", "course_name": "X"}'])
    o, _ = await parse_outline(LLMRouter([stub], sleep=nosleep), "text")
    assert o.course_code == "CS-1" and stub.calls == 2
    assert "CS-1" in summarize(o)
