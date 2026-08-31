"""The rule engine, and the two checks that keep its surface Datalog-compatible.

ADR-0005 fixes a rule surface a semi-naive Datalog engine can take over later. Two
properties make that promise real rather than decorative -- range restriction and
stratification -- and both are checked at load time. The current rule set exercises neither
recursion nor non-trivial stratification, so these tests are the *only* thing standing
between "Datalog-compatible" and "Datalog-shaped".
"""

from __future__ import annotations

import pytest

from oxn.rules.engine import RuleError, check_stratified, check_well_formed, evaluate
from oxn.rules.model import Atom, Compare, Facts, Head, Rule, Var


def _facts() -> Facts:
    facts = Facts()
    facts.add("entity", ("e1", "a.py", "function", "a.f", 10), ("e2", "a.py", "module", "a", 1))
    facts.add("metric", ("e1", "cognitive_complexity", 20.0, "EXACT", "exact"))
    facts.add("metric", ("e2", "cognitive_complexity", 3.0, "EXACT", "exact"))
    facts.add("ceiling", ("cognitive_complexity", "a.py", 12.0))
    facts.add("callable", ("function",), ("method",), ("lambda",))
    facts.add("blocking", ("EXACT", "exact"), ("EXACT", "lower"), ("APPROX", "lower"))
    return facts


def _ceiling_rule(**overrides) -> Rule:
    body = (
        Atom("entity", (Var("E"), Var("Path"), Var("Kind"), Var("Name"), Var("Line"))),
        Atom("metric", (Var("E"), "cognitive_complexity", Var("V"), Var("Ex"), Var("B"))),
        Atom("blocking", (Var("Ex"), Var("B"))),
        Atom("callable", (Var("Kind"),)),
        Atom("ceiling", ("cognitive_complexity", Var("Path"), Var("C"))),
        Compare(Var("V"), ">", Var("C")),
    )
    head = Head(
        path=Var("Path"), entity=Var("Name"), line=Var("Line"), value=Var("V"), ceiling=Var("C")
    )
    return Rule(name="cognitive_complexity", body=body, head=head, **overrides)


# ---- evaluation -------------------------------------------------------------------------


def test_a_satisfied_rule_produces_one_finding_per_binding() -> None:
    found = evaluate([_ceiling_rule()], _facts())
    assert [(f.entity, f.value, f.ceiling) for f in found] == [("a.f", 20.0, 12.0)]


def test_the_kind_guard_excludes_what_the_rule_is_not_about() -> None:
    """The module entity scores 3 and is not callable; neither fact should reach the head."""
    facts = _facts()
    facts.add("metric", ("e2", "cognitive_complexity", 99.0, "EXACT", "exact"))
    assert [f.entity for f in evaluate([_ceiling_rule()], facts)] == ["a.f"]


def test_an_unsound_measurement_cannot_block() -> None:
    """ADR-0002: only EXACT, or APPROX known to be a lower bound, may block a ceiling.

    The policy is a join against the `blocking` relation, so a rule that omits that atom
    reports without blocking rather than silently acquiring the power.
    """
    facts = _facts()
    facts.relations["metric"] = {("e1", "cognitive_complexity", 20.0, "APPROX", "upper")}
    assert evaluate([_ceiling_rule()], facts) == []


def test_a_repeated_variable_in_one_atom_must_match_both_columns() -> None:
    facts = Facts()
    facts.add("edge", ("a", "b"), ("c", "c"))
    rule = Rule(
        name="self_edge",
        body=(Atom("edge", (Var("X"), Var("X"))),),
        head=Head(path=Var("X"), entity=Var("X")),
    )
    assert [f.path for f in evaluate([rule], facts)] == ["c"]


def test_a_negated_atom_filters_and_never_binds() -> None:
    facts = Facts()
    facts.add("edge", ("a", "b"), ("a", "c"))
    facts.add("allowed", ("a", "b"))
    rule = Rule(
        name="disallowed",
        body=(
            Atom("edge", (Var("S"), Var("T"))),
            Atom("allowed", (Var("S"), Var("T")), negated=True),
        ),
        head=Head(path=Var("S"), entity=Var("T")),
    )
    assert [f.entity for f in evaluate([rule], facts)] == ["c"]


def test_a_rule_is_advisory_unless_it_says_otherwise() -> None:
    assert evaluate([_ceiling_rule(blocking=False)], _facts())[0].blocking is False


# ---- the two checks that make the surface translatable ----------------------------------


def test_a_comparison_on_an_unbound_variable_is_refused() -> None:
    rule = Rule("bad", (Compare(Var("X"), ">", 1),), Head(path="p", entity="e"))
    with pytest.raises(RuleError, match="unbound variable"):
        check_well_formed(rule)


def test_a_negated_atom_may_not_introduce_a_variable() -> None:
    """Negation cannot bind: `not p(X)` with X free is unsafe, and Datalog rejects it."""
    rule = Rule(
        "bad",
        (Atom("entity", (Var("E"),)), Atom("other", (Var("Free"),), negated=True)),
        Head(path=Var("E"), entity=Var("E")),
    )
    with pytest.raises(RuleError, match="negated atom"):
        check_well_formed(rule)


def test_a_head_may_not_use_a_variable_the_body_never_bound() -> None:
    rule = Rule("bad", (Atom("entity", (Var("E"),)),), Head(path=Var("Nope"), entity=Var("E")))
    with pytest.raises(RuleError, match="unbound variable"):
        check_well_formed(rule)


def test_negating_a_derived_relation_is_rejected_as_unstratified() -> None:
    """A hand-rolled evaluator will happily run this and return something plausible.

    That is exactly how a surface stops being Datalog-compatible without anyone noticing,
    which is why it raises instead.
    """
    derived = Rule("derived", (Atom("entity", (Var("E"),)),), Head(path=Var("E"), entity=Var("E")))
    negates = Rule(
        "other",
        (Atom("entity", (Var("E"),)), Atom("derived", (Var("E"),), negated=True)),
        Head(path=Var("E"), entity=Var("E")),
    )
    with pytest.raises(RuleError, match="not stratified"):
        check_stratified([derived, negates])


def test_negating_an_extensional_relation_is_fine() -> None:
    """Which is the whole of the current rule set -- see ADR-0005's appendix."""
    rule = Rule(
        "ok",
        (Atom("entity", (Var("E"),)), Atom("allowed", (Var("E"),), negated=True)),
        Head(path=Var("E"), entity=Var("E")),
    )
    check_stratified([rule])


def test_evaluation_refuses_a_malformed_rule_rather_than_skipping_it() -> None:
    """A malformed rule must fail loudly with its name, never be quietly dropped."""
    good = _ceiling_rule()
    bad = Rule("bad", (Compare(Var("X"), ">", 1),), Head(path="p", entity="e"))
    with pytest.raises(RuleError, match="'bad'"):
        evaluate([good, bad], _facts())
