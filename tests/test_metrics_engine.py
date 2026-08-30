"""Metrics attached to graph entities, and persisted with their exactness."""

from __future__ import annotations

from oxn.graph.builder import build_file
from oxn.graph.model import EntityKind
from oxn.graph.store import GraphStore
from oxn.languages import get_parser
from oxn.metrics.engine import measure_file
from oxn.profiles import get_profile

SOURCE = '''
class Service:
    """Doc."""

    def handle(self, request, retries=3):
        if request:
            for _ in range(retries):
                if request.ok:
                    return 1
        return 0
'''.lstrip()


def measured():
    profile = get_profile("python")
    data = SOURCE.encode()
    tree = get_parser("python").parse(data)
    parsed = build_file("svc.py", data, profile, tree.root_node)
    return parsed, measure_file(list(parsed.entities), data, profile, tree.root_node)


def test_every_entity_is_measured() -> None:
    parsed, results = measured()
    assert len(results) == len(parsed.entities)


def test_callables_get_complexity_and_shape_metrics() -> None:
    _, results = measured()
    handle = next(m for m in results if m.qualified_name.endswith("handle"))
    assert handle.kind is EntityKind.METHOD
    assert handle.get("cyclomatic_complexity") == 4
    assert handle.get("cognitive_complexity") == 6
    assert handle.get("max_nesting_depth") == 3
    assert handle.get("exit_points") == 2
    assert handle.get("parameter_count") == 3


def test_non_callables_get_size_metrics_but_not_complexity() -> None:
    _, results = measured()
    cls = next(m for m in results if m.kind is EntityKind.CLASS)
    assert cls.get("sloc") is not None
    assert cls.get("cyclomatic_complexity") is None


def test_cognitive_score_carries_its_explanation() -> None:
    _, results = measured()
    handle = next(m for m in results if m.qualified_name.endswith("handle"))
    trail = handle.values["cognitive_complexity"].explanation
    assert [t.split(" ")[0] for t in trail] == ["+1", "+2", "+3"]
    assert "nested 2 deep" in trail[-1]


def test_metric_values_declare_exactness_and_resolution() -> None:
    """An agent must never be handed an approximation that looks exact (ADR-0002)."""
    _, results = measured()
    handle = next(m for m in results if m.qualified_name.endswith("handle"))
    payload = handle.as_dict()["metrics"]["cognitive_complexity"]
    assert payload["resolution"] == "L0"
    assert "explanation" in payload
    # `handle` calls `range`, so indirect recursion cannot be ruled out without a call
    # graph. The value is therefore a lower bound rather than final.
    assert payload["exactness"] == "APPROX"
    assert payload["bound"] == "lower"


def test_a_function_that_calls_nothing_is_exact() -> None:
    """The recursion rule is the only thing between a Tier-1 score and exactness."""
    from oxn.graph.builder import build_file
    from oxn.languages import get_parser
    from oxn.metrics.engine import measure_file
    from oxn.profiles import get_profile

    profile = get_profile("python")
    data = b"def leaf(a):\n    if a:\n        return 1\n    return 2\n"
    tree = get_parser("python").parse(data)
    parsed = build_file("leaf.py", data, profile, tree.root_node)
    # Match on kind: the module entity is also called `leaf`, and carries no complexity.
    measured_leaf = next(
        m
        for m in measure_file(list(parsed.entities), data, profile, tree.root_node)
        if m.kind is EntityKind.FUNCTION
    )
    value = measured_leaf.values["cognitive_complexity"]
    assert value.exactness == "EXACT"
    assert value.bound == "exact"


def test_a_lower_bound_may_still_block_a_ceiling_gate() -> None:
    """A value that can only grow already proves it exceeds a ceiling it exceeds."""
    _, results = measured()
    handle = next(m for m in results if m.qualified_name.endswith("handle"))
    value = handle.values["cognitive_complexity"]
    assert value.exactness == "APPROX"
    assert value.can_block_ceiling


def test_metrics_round_trip_through_the_store(tmp_path) -> None:
    parsed, results = measured()
    with GraphStore(tmp_path / "g.db") as store:
        store.put_file(parsed, profile_version=1, grammar_version="test")
        store.put_metrics(parsed.path, results)

        stored = store.metrics_for(parsed.path)
        handle = next(m for m in results if m.qualified_name.endswith("handle"))
        assert stored[handle.entity_id]["cognitive_complexity"] == 6

        worst = store.worst("cognitive_complexity", limit=1)
        assert worst[0][0].endswith("handle")
        assert worst[0][2] == 6


def test_reindexing_replaces_metrics_rather_than_accumulating(tmp_path) -> None:
    parsed, results = measured()
    with GraphStore(tmp_path / "g.db") as store:
        store.put_file(parsed, 1, "test")
        store.put_metrics(parsed.path, results)
        before = store.stats()["metrics"]
        store.put_metrics(parsed.path, results)
        assert store.stats()["metrics"] == before
