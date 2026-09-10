"""`oxn calls`: the one surface that has no answer at L0/L1, and says so.

Every other report in OXN means something without a SCIP index. This one does not: nothing
below L2 writes a `calls` edge, because a name resolved by scope rules is a *candidate* and
ADR-0002 will not let a candidate become a fact. So the interesting case here is the empty
one -- a tree nobody has indexed must be told it has no call graph, not handed a confident
`0 dead-code candidates`.

That failure mode has a history in this repository. `--deep` advertised a cycle check that no
fact source populated, `MAX_DUPLICATION_RATIO` sat in `thresholds.py` with no reader while the
docs promised a gate, and `metrics/callgraph.py` itself was complete and called by nothing.
Silent inertness is the recurring defect, and a surface that reports zero for "I cannot tell"
is the same shape wearing an answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oxn.graph.model import Edge, EdgeKind, Provenance, Resolution
from oxn.graph.rows import call_edges
from oxn.report import NO_CALL_GRAPH, run_calls

SOURCE = (
    "def helper():\n    return 1\n\n\n"
    "def _private():\n    return 2\n\n\n"
    "def main():\n    return helper()\n"
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "m.py").write_text(SOURCE)
    return tmp_path


def test_a_tree_with_no_index_is_told_so_rather_than_told_zero(project: Path) -> None:
    payload = run_calls(["."])

    assert payload["status"] == "UNAVAILABLE"
    assert "dead_code_candidates" not in payload, "no answer is not an empty answer"
    assert "oxn index" in payload["note"], "the note must name the remedy"
    assert payload["note"] == NO_CALL_GRAPH


def _with_calls(project: Path) -> dict:
    """Index the tree, then write the call edges a SCIP ingest would have written."""
    from oxn.graph.indexer import Indexer

    with Indexer(root=project) as indexer:
        indexer.index()
        entities = {
            entity.qualified_name: entity.id for entity in indexer.store.entities_for("m.py")
        }
        indexer.store.put_edges(
            "m.py",
            [
                Edge(
                    src_id=entities["m.main"],
                    kind=EdgeKind.CALLS,
                    dst_id=entities["m.helper"],
                    provenance=Provenance.SCIP,
                    resolution=Resolution.L2,
                )
            ],
        )
    return run_calls(["."])


def test_fan_in_dead_code_and_the_caveat_all_reach_the_payload(project: Path) -> None:
    """`_private` is unreachable and `helper` is called; both must be visible as such."""
    payload = _with_calls(project)

    assert payload["status"] == "OK"
    assert payload["edges"] == 1
    fan = {row["qualified_name"]: row for row in payload["fan"]}
    assert fan["m.helper"]["fan_in"] == 1
    assert fan["m.main"]["fan_out"] == 1

    dead = {found["qualified_name"] for found in payload["dead_code_candidates"]}
    assert "m._private" in dead, dead
    assert "m.main" not in dead, "`main` is a root every project has"
    assert "m.helper" not in dead, "reached from a root"


def test_a_call_to_something_outside_the_tree_is_counted_not_dropped(project: Path) -> None:
    """`0 declined` on a corpus that dropped 1,939 of 4,025 edges is the bug this guards.

    The tempting store query filters `dst_id IS NOT NULL`, and then `build_call_graph`'s
    `unresolved_targets` counter -- which exists precisely to say how much of the picture is
    missing -- can only ever report zero.
    """
    from oxn.graph.indexer import Indexer

    with Indexer(root=project) as indexer:
        indexer.index()
        entities = {
            entity.qualified_name: entity.id for entity in indexer.store.entities_for("m.py")
        }
        indexer.store.put_edges(
            "m.py",
            [
                Edge(
                    src_id=entities["m.main"],
                    kind=EdgeKind.CALLS,
                    dst_ref="requests.get",
                    provenance=Provenance.SCIP,
                    resolution=Resolution.L2,
                )
            ],
        )
        assert len(call_edges(indexer.store)) == 1, "the unresolved edge must survive the read"

    assert run_calls(["."])["declined"] == 1
