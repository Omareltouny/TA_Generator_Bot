"""Rules engine unit tests (spec sections 4 and 12): never auto-disable, scope isolation, conflicts, duplicates."""
import pytest
from sqlalchemy import select

from bot.db.models import FeedbackRule, PlanItem
from bot.services import rules
from bot.services.rules import NewRule, RuleInvariantError
from tests.helpers import Scripted, default_conflict, make_llm, seed


@pytest.fixture
async def world(session):
    u, c, items = await seed(session)
    by = {(i.type, i.seq): i for i in items}
    return session, u, c, by


async def add(s, llm, u, c, texts_scopes, *, item=None, kind="feedback", origin="feedback"):
    batch = await rules.create_batch(s, course_id=c.id, user_id=u.id, kind=kind, chat_id=1, regen_item_id=item.id if item else None)
    return batch, await rules.add_rules(s, llm, batch, [NewRule(t, sc) for t, sc in texts_scopes], origin=origin, item=item)


async def statuses(s, course_id):
    return {r.id: r.status for r in (await s.execute(select(FeedbackRule).where(FeedbackRule.course_id == course_id))).scalars()}


# ---- pure parts -------------------------------------------------------------
def test_parse_rules_scopes():
    d = {"rules": [{"text": "a", "scope": "type"}, {"text": " ", "scope": "item"}, {"text": "b", "scope": "weird"}, {"text": "c"}]}
    assert [(r.text, r.scope) for r in rules.parse_rules(d)] == [("a", "type"), ("b", "item"), ("c", "item")]
    assert [r.scope for r in rules.parse_rules(d, allow_type=False)] == ["item", "item", "item"]
    assert [r.scope for r in rules.parse_rules(d, fixed_scope="course")] == ["course"] * 3
    with pytest.raises(ValueError):
        rules.parse_rules({"rules": "x"})


def test_conflict_requires_reason_pure():
    data = {"conflicts": [{"new": 0, "existing": 5, "reason": ""}, {"new": 0, "existing": 5, "reason": "short"},
                          {"new": 0, "existing": 5, "reason": "Needs Java while the other needs Python."},
                          {"new": 0, "existing": 5, "reason": "Needs Java while the other needs Python."},
                          {"new": 9, "existing": 5, "reason": "unknown new index is dropped here"},
                          {"new": 0, "existing": 77, "reason": "unknown existing id is dropped here"}],
            "duplicates": []}
    conflicts, dups = rules.parse_conflicts(data, 1, {5})
    assert conflicts == [(0, 5, "Needs Java while the other needs Python.")] and dups == []
    # a pair flagged both as conflict and duplicate counts as a duplicate
    c2, d2 = rules.parse_conflicts({"conflicts": data["conflicts"][2:3], "duplicates": [{"new": 0, "existing": 5}]}, 1, {5})
    assert c2 == [] and d2 == [(0, 5)]


def test_invariants():
    ok = [FeedbackRule(scope="course"), FeedbackRule(scope="type", item_type="lab"), FeedbackRule(scope="item", item_id=3)]
    for r in ok:
        rules.check_invariants(r)
    for bad in (FeedbackRule(scope="item"), FeedbackRule(scope="item", item_id=1, item_type="lab"), FeedbackRule(scope="type"),
                FeedbackRule(scope="type", item_type="lab", item_id=1), FeedbackRule(scope="course", item_id=1),
                FeedbackRule(scope="galaxy")):
        with pytest.raises(RuleInvariantError):
            rules.check_invariants(bad)


# ---- guarantees -------------------------------------------------------------
async def test_two_feedback_rounds_keep_both(world):
    """The owner's bug: a second round of feedback must not remove the first."""
    s, u, c, by = world
    lab2, llm = by[("lab", 2)], make_llm()
    _, o1 = await add(s, llm, u, c, [("Q3 answer should be 42", "item")], item=lab2)
    _, o2 = await add(s, llm, u, c, [("Shorten question 2 to one sentence", "item")], item=lab2)
    assert not o1.pending and not o2.pending
    app = await rules.applicable_rules(s, c.id, lab2)
    assert [r.rule_text for r in app.item] == ["Q3 answer should be 42", "Shorten question 2 to one sentence"]
    assert set((await statuses(s, c.id)).values()) == {"active"}


async def test_rules_never_auto_disabled(world):
    """False-positive conflicts from the LLM only ever create pending rows; no existing rule changes status."""
    s, u, c, by = world
    prov = Scripted()
    prov.conflict_fn = lambda n, o: "These two rules look contradictory to me for some reason."  # always cries wolf
    llm = make_llm(prov)
    first = (await add(s, make_llm(), u, c, [("Use Python", "course"), ("Keep labs short", "type")], item=by[("lab", 1)]))[1].rules
    before = await statuses(s, c.id)
    for i in range(6):
        scope = ("course", "type", "item")[i % 3]
        _, out = await add(s, llm, u, c, [(f"Some new instruction {i}", scope)], item=by[("lab", 1)])
        after = await statuses(s, c.id)
        for rid, st in before.items():
            assert after[rid] == st, "an existing rule changed status without a user action"
        before = after
    for r in first:
        await s.refresh(r)
        assert r.status == "active"
    assert any(st == "pending" for st in before.values())  # the false positives are held for the user, not applied


async def test_scope_isolation(world):
    s, u, c, by = world
    llm = make_llm()
    await add(s, llm, u, c, [("Q3 answer should be 42", "item")], item=by[("lab", 1)])
    await add(s, llm, u, c, [("Labs use pseudo code", "type")], item=by[("lab", 1)])
    await add(s, llm, u, c, [("Use metric units", "course")], item=by[("lab", 1)])
    texts = lambda a: ([r.rule_text for r in a.course], [r.rule_text for r in a.type], [r.rule_text for r in a.item])
    a1 = await rules.applicable_rules(s, c.id, by[("lab", 1)])
    a2 = await rules.applicable_rules(s, c.id, by[("lab", 2)])
    t1 = await rules.applicable_rules(s, c.id, by[("assignment", 1)])
    assert texts(a1) == (["Use metric units"], ["Labs use pseudo code"], ["Q3 answer should be 42"])
    assert texts(a2) == (["Use metric units"], ["Labs use pseudo code"], [])       # item rule absent from other labs
    assert texts(t1) == (["Use metric units"], [], [])                             # lab rule absent from other types


async def test_blocking_vs_informational(world):
    s, u, c, by = world
    llm, lab1 = make_llm(), by[("lab", 1)]
    await add(s, llm, u, c, [("Use Java", "course")])
    # different scope: informational override, the new rule is active
    _, o = await add(s, llm, u, c, [("Use Python", "item")], item=lab1)
    assert not o.pending and len(o.overrides) == 1 and "Python" in o.overrides[0].reason
    assert o.rules[0].status == "active"
    # same scope: blocking, new rule pending with reason, old untouched
    _, o2 = await add(s, llm, u, c, [("Use Python", "course")])
    new = o2.pending[0]
    assert new.status == "pending" and new.pending_reason == "conflict" and "Python" in new.conflict_reason
    old = await s.get(FeedbackRule, new.conflicts_with)
    assert old.rule_text == "Use Java" and old.status == "active"


async def test_conflict_without_reason_is_dropped(world):
    s, u, c, by = world
    prov = Scripted()
    prov.conflict_fn = lambda n, o: ""  # no reason -> must not block anything
    await add(s, make_llm(), u, c, [("Use Java", "course")])
    _, o = await add(s, make_llm(prov), u, c, [("Use Python", "course")])
    assert not o.pending and o.rules[0].status == "active"


async def test_duplicate_not_stored(world):
    s, u, c, by = world
    llm = make_llm()
    await add(s, llm, u, c, [("Use Python", "course")])
    _, o = await add(s, llm, u, c, [("use python", "course")])
    assert o.rules == [] and len(o.duplicates) == 1 and o.duplicates[0][1].rule_text == "Use Python"
    assert len((await statuses(s, c.id))) == 1
    # a narrower existing rule does not "cover" a broader new one: the broader rule is kept
    await add(s, llm, u, c, [("Add a summary", "item")], item=by[("lab", 1)])
    _, o3 = await add(s, llm, u, c, [("add a summary", "course")])
    assert len(o3.rules) == 1 and not o3.duplicates


async def test_resolution_choices_and_single_resolution(world):
    s, u, c, by = world
    llm = make_llm()
    await add(s, llm, u, c, [("Use Java", "course")])
    batch, o = await add(s, llm, u, c, [("Use Python", "course"), ("Add marks everywhere", "course")], item=by[("lab", 1)])
    new = o.pending[0]
    assert o.resolved_batch is None and batch.resolved is False       # regeneration must wait
    for choice, old_status, new_status in (("old", "active", "disabled"), ("both", "active", "active"), ("new", "disabled", "active")):
        # rebuild the situation for each choice
        await s.refresh(new)
        new.status, new.pending_reason, new.conflicts_with = "pending", "conflict", (await s.execute(
            select(FeedbackRule.id).where(FeedbackRule.rule_text == "Use Java"))).scalar_one()
        old = await s.get(FeedbackRule, new.conflicts_with)
        old.status = "active"
        new.conflict_reason, batch.resolved = "x" * 12, False
        await s.commit()
        resolved = await rules.resolve_conflict(s, new.id, choice)
        await s.refresh(old); await s.refresh(new)
        assert (old.status, new.status) == (old_status, new_status), choice
        assert resolved is not None and resolved.id == batch.id
        if choice == "old":
            assert new.disabled_reason == f"rejected: you kept #{old.id}"
        if choice == "new":
            assert old.replaced_by == new.id and old.disabled_reason == f"replaced by #{new.id} (you chose Keep new)"
    # once resolved, further operations do not hand the batch out again (no second regeneration)
    assert await rules.resolve_conflict(s, new.id, "new") is None


async def test_regen_waits_and_fires_once(world):
    s, u, c, by = world
    llm, lab1 = make_llm(), by[("lab", 1)]
    await add(s, llm, u, c, [("Use Java", "course"), ("Use C", "course")])
    prov = Scripted()
    prov.conflict_fn = lambda n, o: "Both rules demand a different language for the course." if "Use" in n else None
    batch, o = await add(s, make_llm(prov), u, c, [("Use Python", "course")], item=lab1)
    assert len(o.pending) == 1 and o.resolved_batch is None
    assert (await rules.resolve_conflict(s, o.pending[0].id, "both")) is not None
    assert await rules.resolve_conflict(s, o.pending[0].id, "both") is None


async def test_edit_scope_disable_restore_delete(world):
    s, u, c, by = world
    llm = make_llm()
    _, o = await add(s, llm, u, c, [("Use Java", "course")])
    java = o.rules[0]
    _, o2 = await add(s, llm, u, c, [("Use Python", "item")], item=by[("lab", 1)])
    py = o2.rules[0]
    # widening the item rule to course scope now clashes (blocking): becomes pending, Java untouched
    out = await rules.change_scope(s, llm, py.id, "course")
    assert py.status == "pending" and py.scope == "course" and py.item_id is None
    await s.refresh(java); assert java.status == "active"
    # editing the pending rule to something harmless re-checks it and activates it
    out = await rules.edit_rule_text(s, llm, py.id, "Add marks to every question")
    assert py.status == "active" and py.pending_reason is None
    # disable / restore / delete safety
    await rules.disable_rule(s, py.id)
    assert py.status == "disabled" and py.disabled_reason == "disabled by you"
    with pytest.raises(ValueError):
        await rules.delete_rule_permanently(s, java.id)   # active rules cannot be deleted directly
    await rules.restore_rule(s, llm, py.id)
    assert py.status == "active" and py.disabled_reason is None
    await rules.disable_rule(s, py.id)
    await rules.delete_rule_permanently(s, py.id)
    assert await s.get(FeedbackRule, py.id) is None


async def test_busy_llm_stores_nothing(world):
    s, u, c, by = world
    cid = c.id
    await add(s, make_llm(), u, c, [("Use Java", "course")])
    from bot.services.llm_router import AllProvidersBusy
    with pytest.raises(AllProvidersBusy):
        await add(s, make_llm(Scripted(busy=True)), u, c, [("Use Python", "course")])
    await s.rollback()
    assert len(await statuses(s, cid)) == 1


async def test_chunking_merges_results(world, monkeypatch):
    s, u, c, by = world
    monkeypatch.setattr(rules, "CHUNK", 2)
    llm = make_llm()
    await add(s, llm, u, c, [("Rule A", "course"), ("Rule B", "course"), ("Use Java", "course"), ("Rule D", "course")])
    prov = Scripted()
    _, o = await add(s, make_llm(prov), u, c, [("Use Python", "course")])
    assert len(prov.system_calls) == 2 and len(o.pending) == 1
