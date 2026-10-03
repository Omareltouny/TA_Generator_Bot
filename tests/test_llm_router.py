import pytest

from bot.services.llm_router import (AllProvidersBusy, FatalError, LLMRouter, RetryableError,
                                     extract_json)


class Stub:
    def __init__(self, name, models, behaviours):
        self.name, self.models, self.b, self.calls = name, models, list(behaviours), 0

    async def complete(self, model, system, prompt, json_mode, max_tokens=4096):
        self.calls += 1
        x = self.b.pop(0) if self.b else "ok"
        if isinstance(x, Exception):
            raise x
        return x


async def nosleep(_): pass


async def test_fallback_on_429():
    a = Stub("a", ["m"], [RetryableError("429", retry_after=30)])
    b = Stub("b", ["m"], ["hello"])
    r = await LLMRouter([a, b], sleep=nosleep).complete("hi")
    assert (r.text, r.provider) == ("hello", "b")


async def test_retry_then_succeed_same_provider():
    a = Stub("a", ["m"], [RetryableError("500"), "fine"])
    r = await LLMRouter([a], sleep=nosleep).complete("hi")
    assert r.text == "fine" and a.calls == 2


async def test_fatal_skips_model():
    a = Stub("a", ["m1", "m2"], [FatalError("bad key"), "from m2"])
    r = await LLMRouter([a], sleep=nosleep).complete("hi")
    assert (r.model, r.text) == ("m2", "from m2")


async def test_all_busy_and_cooldown():
    t = [0.0]
    a = Stub("a", ["m"], [RetryableError("429", retry_after=45)])
    router = LLMRouter([a], sleep=nosleep, clock=lambda: t[0])
    with pytest.raises(AllProvidersBusy):
        await router.complete("x")
    with pytest.raises(AllProvidersBusy) as e:  # cooled down: provider not hit again
        await router.complete("x")
    assert a.calls == 1 and e.value.retry_after == 45
    t[0] = 50
    assert (await router.complete("x")).text == "ok"


async def test_complete_json_repairs():
    a = Stub("a", ["m"], ["not json", '```json\n{"title": "T"}\n```'])
    def v(d): d["title"]
    data, _ = await LLMRouter([a], sleep=nosleep).complete_json("p", validate=v)
    assert data == {"title": "T"}


def test_extract_json_with_prose():
    assert extract_json('Sure! {"a": 1} hope that helps') == {"a": 1}
