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
