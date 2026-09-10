"""Tier-3 metrics: cohesion, the CK suite, and the call graph.

Every fixture here has an answer computable by hand, because these metrics have several
incompatible published definitions and "it matches the tool we compared against" is not the
same as "it implements the definition it claims to".
"""

from __future__ import annotations

import pytest

from oxn.languages import get_parser
from oxn.metrics.callgraph import build_call_graph, default_roots, unreachable
from oxn.metrics.cohesion import cohesion
from oxn.metrics.coupling import Evidence, build_hierarchy, ck_metrics
from oxn.profiles import get_profile
from oxn.resolve.members import build_class_models
from oxn.resolve.scopes import build_scopes


def models(source: str, language: str = "python"):
    profile = get_profile(language)
    root = get_parser(language).parse(source.encode()).root_node
    return build_class_models(root, profile, build_scopes(root, profile))


def only(source: str, name: str = "C"):
    return models(source)[name]


# ---- member extraction ---------------------------------------------------------------------


def test_fields_come_from_the_class_body_and_receiver_writes() -> None:
    model = only("class C:\n    declared = 1\n    def __init__(self):\n        self.assigned = 2\n")
    assert model.fields == {"declared", "assigned"}


def test_the_receiver_is_whatever_the_first_parameter_is_called() -> None:
    """A class using `myself` instead of `self` must not look maximally incohesive."""
    model = only(
        "class C:\n"
        "    def __init__(myself):\n        myself.x = 1\n"
        "    def read(myself):\n        return myself.x\n"
    )
    assert model.fields == {"x"}
    assert model.methods["read"].reads == {"x"}


def test_intra_class_calls_are_recorded_separately_from_field_access() -> None:
    model = only(
        "class C:\n    def a(self):\n        return self.b()\n    def b(self):\n        return 1\n"
    )
    assert model.methods["a"].calls == {"b"}
    assert model.methods["a"].reads == set()


def test_an_inherited_or_dynamic_access_is_counted_not_dropped() -> None:
    """The honesty valve: a number a consumer can weigh, not a silent omission."""
    model = only("class C:\n    def m(self):\n        return self.from_a_base\n")
    assert model.unresolved_accesses == 1
    assert model.is_approximate


def test_properties_are_identified() -> None:
    model = only(
        "class C:\n"
        "    def __init__(self):\n        self.x = 1\n"
        "    @property\n    def value(self):\n        return self.x\n"
    )
    assert model.properties == {"value"}


# ---- LCOM ------------------------------------------------------------------------------------


def test_a_perfectly_cohesive_class() -> None:
    measures = cohesion(
        only(
            "class C:\n"
            "    def __init__(self):\n        self.x = 1\n"
            "    def a(self):\n        return self.x\n"
            "    def b(self):\n        return self.x + 1\n"
        )
    )
    assert measures.lcom1 == 0
    assert measures.lcom3 == 1
    assert measures.lcom_star == pytest.approx(0.0)
    assert measures.exactness == "EXACT"


def test_a_class_with_no_shared_state_at_all() -> None:
    measures = cohesion(
        only(
            "class C:\n"
            "    def p(self, x):\n        return x\n"
            "    def q(self, y):\n        return y\n"
            "    def r(self, z):\n        return z\n"
        )
    )
    assert measures.lcom1 == 3  # every pair of three methods shares nothing
    assert measures.lcom3 == 3  # three separate components
    assert measures.lcom4 == 3


def test_lcom4_joins_what_a_call_connects() -> None:
    """LCOM4 = LCOM3 plus intra-class calls, which is what makes it the actionable one."""
    source = (
        "class C:\n"
        "    def __init__(self):\n        self.x = 1\n"
        "    def a(self):\n        return self.helper()\n"
        "    def helper(self):\n        return 2\n"
    )
    measures = cohesion(only(source))
    assert measures.lcom4 < measures.lcom3


def test_lcom_star_is_undefined_rather_than_invented() -> None:
    """A single method, or no fields, carries no cohesion information to report."""
    assert cohesion(only("class C:\n    def a(self):\n        pass\n")).lcom_star is None
    assert (
        cohesion(
            only(
                "class C:\n    def a(self, x):\n        return x\n"
                "    def b(self, y):\n        return y\n"
            )
        ).lcom_star
        is None
    )


def test_lcom_star_exceeds_one_when_fields_are_never_touched() -> None:
    """Legitimate under Henderson-Sellers, and a real signal about data-only classes."""
    measures = cohesion(
        only(
            "class C:\n"
            "    a = 1\n    b = 2\n    c = 3\n"
            "    def m(self):\n        return self.a\n"
            "    def n(self):\n        return self.a\n"
        )
    )
    assert measures.lcom_star is not None
    assert measures.lcom_star > 1.0


def test_connectivity_needs_three_methods() -> None:
    assert cohesion(only("class C:\n    def a(self):\n        pass\n")).connectivity is None


# ---- CK ---------------------------------------------------------------------------------------


HIERARCHY_SOURCE = (
    "class Base:\n    def run(self):\n        pass\n\n\n"
    "class Middle(Base):\n    def run(self):\n        pass\n\n\n"
    "class Leaf(Middle):\n    def run(self):\n        pass\n\n\n"
    "class External(SomethingElsewhere):\n    def go(self):\n        pass\n"
)


@pytest.fixture
def ck():
    found = models(HIERARCHY_SOURCE)
    hierarchy = build_hierarchy({"m.py": found})
    return {name: ck_metrics(model, hierarchy) for name, model in found.items()}


def test_depth_of_inheritance(ck) -> None:
    assert ck["Base"].dit == 0
    assert ck["Middle"].dit == 1
    assert ck["Leaf"].dit == 2


def test_dit_is_the_longest_path_and_not_the_number_of_ancestors() -> None:
    """A chain cannot tell the two apart, and every fixture here was a chain.

    DIT counted the distinct classes it had visited, so `X(A, B)` with `A(R)` and `B(R)`
    reported **4** where the longest path to a root is 2. Python allows multiple inheritance
    and Java's `extends A implements I1, I2` produces the same shape as a matter of routine,
    so this was the normal case rather than an exotic one.
    """
    source = (
        "class R:\n    def r(self):\n        pass\n\n\n"
        "class A(R):\n    def a(self):\n        pass\n\n\n"
        "class B(R):\n    def b(self):\n        pass\n\n\n"
        "class X(A, B):\n    def x(self):\n        pass\n"
    )
    found = models(source)
    hierarchy = build_hierarchy({"m.py": found})
    depths = {name: ck_metrics(model, hierarchy).dit for name, model in found.items()}

    assert depths == {"R": 0, "A": 1, "B": 1, "X": 2}


def test_a_hierarchy_that_cannot_exist_still_terminates() -> None:
    """`A(B)` and `B(A)` parses, and no language would accept it.

    Nothing here should hang or recurse forever on a source file that is syntactically fine
    and semantically impossible: the walk refuses to re-enter a class already on the path
    rather than trusting the declarations to form a tree.
    """
    source = (
        "class A(B):\n    def a(self):\n        pass\n\n\n"
        "class B(A):\n    def b(self):\n        pass\n"
    )
    found = models(source)
    hierarchy = build_hierarchy({"m.py": found})

    assert ck_metrics(found["A"], hierarchy).dit == 1


def test_number_of_children(ck) -> None:
    assert ck["Base"].noc == 1
    assert ck["Middle"].noc == 1
    assert ck["Leaf"].noc == 0


def test_an_external_base_is_flagged_not_silently_treated_as_a_root(ck) -> None:
    """A class extending a library type is deeper than the tree can see, and says so."""
    assert ck["External"].dit_external_unresolved
    assert ck["External"].exactness == "APPROX"
    assert not ck["Base"].dit_external_unresolved


def test_wmc_defaults_to_counting_methods(ck) -> None:
    assert ck["Leaf"].wmc == ck["Leaf"].nom == 1


def test_wmc_can_be_weighted_by_complexity() -> None:
    found = models("class C:\n    def a(self):\n        pass\n    def b(self):\n        pass\n")
    hierarchy = build_hierarchy({"m.py": found})
    weighted = ck_metrics(found["C"], hierarchy, Evidence(complexity={"a": 5, "b": 3}))
    assert weighted.wmc == 8
    assert weighted.nom == 2


# ---- call graph ---------------------------------------------------------------------------------


def test_only_confident_edges_enter_the_call_graph() -> None:
    """A low-confidence guess would invent a cycle and inflate every score inside it."""
    graph = build_call_graph(
        [
            ("a", "b", "L2", 1.0),
            ("b", "c", "L1", 1.0),
            ("c", "a", "L1", 0.25),  # a guess among four candidates
            ("d", None, "L1", 1.0),
        ]
    )
    assert graph.edges["b"] == {"c"}
    assert "a" not in graph.edges.get("c", set())
    assert graph.unresolved_targets == 2
    assert graph.recursion_cycles() == []


def test_indirect_recursion_is_a_cycle() -> None:
    graph = build_call_graph([("a", "b", "L2", 1.0), ("b", "c", "L2", 1.0), ("c", "a", "L2", 1.0)])
    assert graph.recursion_cycles() == [["a", "b", "c"]]
    assert graph.in_recursion() == {"a", "b", "c"}


def test_fan_in_and_fan_out() -> None:
    graph = build_call_graph([("a", "c", "L2", 1.0), ("b", "c", "L2", 1.0), ("c", "d", "L2", 1.0)])
    assert graph.fan_in("c") == 2
    assert graph.fan_out("c") == 1


def test_unreachable_entities_are_candidates_from_declared_roots() -> None:
    graph = build_call_graph([("main", "used", "L2", 1.0)])
    entities = {
        "main": ("m.main", "m.py", "function"),
        "used": ("m.used", "m.py", "function"),
        "orphan": ("m._orphan", "m.py", "function"),
    }
    found = unreachable(graph, entities, ["main"])
    assert [candidate.qualified_name for candidate in found] == ["m._orphan"]


def test_tests_and_public_names_are_roots() -> None:
    """Treating tests as dead code would bury the report in noise."""
    entities = {
        "t": ("tests.test_thing", "tests/test_a.py", "function"),
        "p": ("m.public", "m.py", "function"),
        "h": ("m._hidden", "m.py", "function"),
    }
    roots = default_roots(entities)
    assert "t" in roots
    assert "p" in roots
    assert "h" not in roots


def test_no_tier_3_metric_is_gateable() -> None:
    """The line that keeps ADR-0002's Go accuracy finding a reporting caveat rather than a
    gating bug.

    L0/L1 call resolution is 88.3% precise when *certain* on Go against 99.8% on Python, and
    11.7% of go-kit's confident answers are confidently wrong -- receiver dispatch L1 cannot
    see. Those numbers flow into CBO and RFC. They are harmless because CK metrics are
    reported and never gated: `GATED_METRICS` is Tier-1 only, and `oxn.yaml` rejects
    `ceilings: {cbo: 10}` by name rather than accepting it and gating on a guess.

    That is a property, not a promise. Adding a Tier-3 metric to `GATED_METRICS` would let a
    Go project block a build on a signal wrong about one time in nine, so this test fails
    first and sends the reader to ADR-0002's amendment.
    """
    from oxn.config import GATED_METRICS

    tier_3 = {"cbo", "rfc", "wmc", "dit", "noc", "lcom1", "lcom4", "lcom_star", "connectivity"}

    assert not (tier_3 & set(GATED_METRICS)), (
        "a Tier-3 metric became gateable; L0/L1 call resolution is only 88.3% precise when "
        "certain on Go, so read ADR-0002's 2026-09-06 amendment before allowing this"
    )
