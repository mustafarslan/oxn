"""Dead-code candidates against vulture."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.oracle


@pytest.mark.oracle
def test_dead_code_candidates_overlap_with_vulture(tmp_path) -> None:
    """Characterisation, not equality -- the two tools look for different things.

    vulture reports unused *variables, imports and attributes* as well as functions, and
    works per-file without a call graph. OXN reports entities unreachable from declared
    roots. Only unreached functions are comparable, and even there the two disagree about
    what counts as a root. The assertion is that OXN finds the obvious case.
    """
    vulture = pytest.importorskip("vulture")

    source = tmp_path / "m.py"
    source.write_text(
        "def used():\n    return 1\n\n\n"
        "def _never_called():\n    return 2\n\n\n"
        "def main():\n    return used()\n"
    )

    scanner = vulture.Vulture(verbose=False)
    scanner.scavenge([str(source)])
    theirs = {item.name for item in scanner.get_unused_code()}

    from oxn.graph.builder import build_file
    from oxn.languages import get_parser
    from oxn.metrics.callgraph import build_call_graph, default_roots, unreachable
    from oxn.profiles import get_profile

    profile = get_profile("python")
    data = source.read_bytes()
    tree = get_parser("python").parse(data)
    parsed = build_file("m.py", data, profile, tree.root_node)
    entities = {
        entity.id: (entity.qualified_name, "m.py", entity.kind.value)
        for entity in parsed.entities
        if entity.kind.value in {"function", "method", "class"}
    }
    graph = build_call_graph([])
    ours = {
        candidate.qualified_name.rsplit(".", 1)[-1]
        for candidate in unreachable(graph, entities, default_roots(entities))
    }

    assert "_never_called" in theirs, "vulture should flag the obvious case"
    assert "_never_called" in ours, "OXN should flag it too"
    assert "main" not in ours, "main is a declared root"


def _maps(parsed):
    """The five views of a parsed file that `unreachable` and its inputs each want."""
    entities = list(parsed.entities)
    rows = {entity.id: (entity.qualified_name, "m.py", entity.kind.value) for entity in entities}
    return (
        {entity.qualified_name: entity for entity in entities},
        rows,
        {key: row for key, row in rows.items() if row[2] in {"function", "method"}},
        {key: row for key, row in rows.items() if row[2] == "class"},
        {entity.id: entity.parent_id for entity in entities},
    )


@pytest.mark.oracle
def test_neither_tool_flags_a_helper_a_constructor_calls(tmp_path) -> None:
    """The fixture the first test could not express, because it has no class.

    vulture reads `self._helper()` as a use and says nothing. OXN needed the class to be a
    node in its traversal to agree: `Shown()` resolves to the *class*, so `__init__` has no
    inbound call edge, and before that model both it and `_helper` were reported dead. This
    is the shape that made 138 of 138 candidates on `python-httpx` false positives, and the
    oracle is here so that a regression shows up as a disagreement with another tool rather
    than as a number nobody rechecks.
    """
    vulture = pytest.importorskip("vulture")

    source = tmp_path / "m.py"
    source.write_text(
        "class Shown:\n"
        "    def __init__(self):\n        self._helper()\n\n"
        "    def _helper(self):\n        return 1\n\n\n"
        "def main():\n    return Shown()\n"
    )

    scanner = vulture.Vulture(verbose=False)
    scanner.scavenge([str(source)])
    assert "_helper" not in {item.name for item in scanner.get_unused_code()}

    from oxn.graph.builder import build_file
    from oxn.languages import get_parser
    from oxn.metrics.callgraph import (
        build_call_graph,
        default_roots,
        dispatched_members,
        unreachable,
    )
    from oxn.profiles import get_profile

    profile = get_profile("python")
    data = source.read_bytes()
    tree = get_parser("python").parse(data)
    parsed = build_file("m.py", data, profile, tree.root_node)
    by_name, rows, callables, classes, parents = _maps(parsed)

    graph = build_call_graph(
        [
            (by_name["m.main"].id, by_name["m.Shown"].id, "L2", 1.0),
            (by_name["m.Shown.__init__"].id, by_name["m.Shown._helper"].id, "L2", 1.0),
        ]
    )
    ours = {
        candidate.qualified_name
        for candidate in unreachable(
            graph,
            callables,
            default_roots(rows),
            dispatch=dispatched_members(callables, classes, parents),
        )
    }

    assert ours == set(), f"nothing here is dead, and OXN said {ours}"
