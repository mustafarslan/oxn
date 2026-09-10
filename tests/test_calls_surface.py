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

from oxn.calls import NO_CALL_GRAPH, NO_PRIVACY, run_calls
from oxn.graph.model import Edge, EdgeKind, Provenance, Resolution
from oxn.graph.rows import call_edges

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
    assert "dead_code" not in payload, "no answer is not an empty answer"
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

    dead = {found["qualified_name"] for found in payload["dead_code"]["candidates"]}
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


def _index_and_link(project: Path, edges: list[tuple[str, str]]) -> dict:
    """Index the tree, then write the `(caller, callee)` edges a SCIP ingest would have."""
    from oxn.graph.indexer import Indexer

    with Indexer(root=project) as indexer:
        indexer.index()
        found: dict[str, str] = {}
        for path in indexer.store.known_paths():
            for entity in indexer.store.entities_for(path):
                found[entity.qualified_name] = entity.id
        for path in indexer.store.known_paths():
            written = [
                Edge(
                    src_id=found[source],
                    kind=EdgeKind.CALLS,
                    dst_id=found[target],
                    provenance=Provenance.SCIP,
                    resolution=Resolution.L2,
                )
                for source, target in edges
                if found[source] in {entity.id for entity in indexer.store.entities_for(path)}
            ]
            if written:
                indexer.store.put_edges(path, written)
    return run_calls(["."])


def _dead(payload: dict) -> set[str]:
    return {found["qualified_name"] for found in payload["dead_code"]["candidates"]}


def test_a_helper_called_only_from_a_constructor_is_not_dead(tmp_path, monkeypatch) -> None:
    """The defect that made every row of the httpx report a false positive.

    Nothing writes `Shown.__init__` at a call site -- `Shown()` names the *class*, which is
    what SCIP resolves -- so the constructor had no inbound edge, and `_helper`, reached
    only from it, was reported dead along with it. 124 of httpx's 138 candidates were such
    members and the remaining 14 hung off them: the whole report, 138 of 138.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "m.py").write_text(
        "class Shown:\n"
        "    def __init__(self):\n        self._helper()\n\n"
        "    def _helper(self):\n        return 1\n\n\n"
        "def main():\n    return Shown()\n"
    )
    payload = _index_and_link(
        tmp_path, [("m.main", "m.Shown"), ("m.Shown.__init__", "m.Shown._helper")]
    )

    assert "m.Shown._helper" not in _dead(payload), _dead(payload)
    assert "m.Shown.__init__" not in _dead(payload)


def test_a_private_class_nobody_constructs_is_still_reported(tmp_path, monkeypatch) -> None:
    """Why a class is a *node* in the traversal and not simply a root.

    Rooting every dispatched member outright would have been one line shorter and would have
    hidden this: nothing constructs `_Hidden`, so its constructor is unreachable and must be
    said to be. The reachability of a dispatched member is the reachability of its class.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "m.py").write_text(
        "class _Hidden:\n    def __init__(self):\n        self._helper()\n\n"
        "    def _helper(self):\n        return 1\n\n\n"
        "def main():\n    return 1\n"
    )
    payload = _index_and_link(tmp_path, [("m._Hidden.__init__", "m._Hidden._helper")])

    assert "m._Hidden.__init__" in _dead(payload), _dead(payload)
    assert "m._Hidden._helper" in _dead(payload)


def test_a_call_written_at_module_scope_reaches_what_it_calls() -> None:
    """`if __name__ == "__main__": _main()` -- a call with no enclosing definition.

    `_enclosing_entity` walks up the CST for a definition and found none, so `join` dropped
    the edge and every script entry point so invoked was reported dead. The file's own
    entity is the caller, because the file body is what runs it. Recovering these raised
    call coverage on OXN's own tree from 92.64% to 98.75%.
    """
    from oxn.graph.model import EntityKind
    from oxn.metrics.callgraph import build_call_graph, default_roots, unreachable

    entities = {
        "file": ("m", "m.py", EntityKind.MODULE.value),
        "entry": ("m._main", "m.py", EntityKind.FUNCTION.value),
    }
    graph = build_call_graph([("file", "entry", "L2", 1.0)])
    reported = {
        found.qualified_name for found in unreachable(graph, entities, default_roots(entities))
    }

    assert "m._main" not in reported, "the file body is a root, and it calls `_main`"


def test_a_language_whose_privacy_needs_a_node_refuses_rather_than_reporting_zero() -> None:
    """Java, Rust and TypeScript spell privacy with a modifier, not with the name.

    `default_roots` roots whatever it cannot show to be private, so on those languages it
    roots *everything* and the total is zero without having looked -- the confidently-wrong
    zero this surface exists to refuse. Go is the control: lowercase is unexported, which is
    a name rule, so Go must still get a real answer.
    """
    from oxn.calls import _dead_code
    from oxn.metrics.callgraph import name_privacy

    java = {"a": ("com.example.Thing.helper", "src/Thing.java", "method")}
    go = {"b": ("pkg.doThing", "pkg/thing.go", "function")}

    assert name_privacy(java) == {"a": None}, "a Java name cannot answer this"
    assert name_privacy(go) == {"b": True}, "a lowercase Go name is unexported"

    refused = _dead_code([], name_privacy(java), java, 20)
    assert refused["status"] == "UNAVAILABLE", refused
    assert refused["note"] == NO_PRIVACY
    assert refused["not_judged"] == ["java"]
    assert _dead_code([], name_privacy(go), go, 20)["status"] == "OK", "Go can be answered"


def test_one_judgeable_language_does_not_vouch_for_the_others() -> None:
    """The guard is per language, because polyglot is the normal case.

    Asked per *tree*, a single Python helper beside forty thousand lines of Java satisfies
    it, and Java's structural zero -- every callable rooted because no name can show it
    private -- gets reported as a real answer.
    """
    from oxn.calls import _dead_code
    from oxn.metrics.callgraph import name_privacy

    mixed = {
        "a": ("com.example.Thing.helper", "src/Thing.java", "method"),
        "b": ("tool._helper", "tool.py", "function"),
    }
    section = _dead_code([], name_privacy(mixed), mixed, 20)

    assert section["status"] == "OK", "Python can still be answered"
    assert section["not_judged"] == ["java"], "and Java must be named as unanswered"


def test_paths_choose_which_rows_are_shown_and_not_what_is_computed(tmp_path, monkeypatch) -> None:
    """`oxn calls src/` answered about the whole tree -- silently, and in every count.

    Reachability has to stay whole-tree: the caller that decides whether something under
    `src/` is dead is routinely somewhere else. So the graph is always the whole cache and
    the argument selects rows -- which means `callables` must be selected too, or the header
    counts a population the sections beneath it do not.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "def used():\n    return 1\n\n\n"
        "def _dead_here():\n    return 2\n\n\n"
        "def main():\n    return used()\n"
    )
    (tmp_path / "other" / "b.py").write_text("def _dead_there():\n    return 3\n")

    whole = _index_and_link(tmp_path, [("src.a.main", "src.a.used")])
    scoped = run_calls(["src"])

    assert _dead(whole) == {"src.a._dead_here", "other.b._dead_there"}
    assert _dead(scoped) == {"src.a._dead_here"}, "a row outside the requested path is not shown"
    assert scoped["callables"] < whole["callables"], "the header counts what the sections show"
    assert scoped["declined"] == whole["declined"], "the graph itself is unchanged"


def test_a_function_used_as_a_value_is_not_dead(tmp_path, monkeypatch) -> None:
    """The last of the three false-positive sources, and the largest after dunders.

    A callable in a dispatch table, a callback or a sort key has no call node, so a
    reachability pass that follows only calls cannot see it -- and everything reachable only
    through it goes with it. On OXN's own `src/` this was **36 of the 38 roots of the dead
    forest**, and fixing it took the report from 69 candidates to 4.

    Both shapes are here because they reach differently. `_HANDLERS` is module-scope, so the
    reference comes from the file entity, which is a root. The dict inside `dispatch` is
    function-scope, so the reference comes from `dispatch` and only counts if `dispatch`
    itself is reached.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "m.py").write_text(
        "def _handler():\n    return 1\n\n\n"
        "def _nested():\n    return 2\n\n\n"
        "_HANDLERS = (_handler,)\n\n\n"
        "def dispatch():\n    return {'k': _nested}\n"
    )
    from oxn.graph.indexer import Indexer

    with Indexer(root=tmp_path) as indexer:
        indexer.index()
        found = {entity.qualified_name: entity.id for entity in indexer.store.entities_for("m.py")}
        indexer.store.put_edges(
            "m.py",
            [
                Edge(
                    src_id=found[source],
                    kind=EdgeKind.REFERENCES,
                    dst_id=found[target],
                    provenance=Provenance.SCIP,
                    resolution=Resolution.L2,
                )
                for source, target in (("m", "m._handler"), ("m.dispatch", "m._nested"))
            ]
            + [
                Edge(
                    src_id=found["m"],
                    kind=EdgeKind.CALLS,
                    dst_id=found["m.dispatch"],
                    provenance=Provenance.SCIP,
                    resolution=Resolution.L2,
                )
            ],
        )
    payload = run_calls(["."])

    assert _dead(payload) == set(), f"nothing here is unused, and OXN said {_dead(payload)}"


def test_a_reference_is_not_a_call(tmp_path, monkeypatch) -> None:
    """`REFERENCES` reaches, and must not count.

    Fan-in, fan-out and the recursion increment are counts of calls. A function named in a
    handler table has not been called once, and merging these into `CallGraph` would have
    given every such name a caller it does not have -- and could manufacture a recursion
    cycle, which inflates every cognitive-complexity score in it.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "m.py").write_text("def _handler():\n    return 1\n\n\n_H = (_handler,)\n")
    from oxn.graph.indexer import Indexer

    with Indexer(root=tmp_path) as indexer:
        indexer.index()
        found = {entity.qualified_name: entity.id for entity in indexer.store.entities_for("m.py")}
        indexer.store.put_edges(
            "m.py",
            [
                Edge(
                    src_id=found["m"],
                    kind=EdgeKind.REFERENCES,
                    dst_id=found["m._handler"],
                    provenance=Provenance.SCIP,
                    resolution=Resolution.L2,
                ),
                Edge(
                    src_id=found["m"],
                    kind=EdgeKind.CALLS,
                    dst_ref="requests.get",
                    provenance=Provenance.SCIP,
                    resolution=Resolution.L2,
                ),
            ],
        )
    payload = run_calls(["."])

    assert "m._handler" not in _dead(payload), "the reference must reach it"
    fan = {row["qualified_name"]: row for row in payload["fan"]}
    assert fan["m._handler"]["fan_in"] == 0, "and must not give it a caller"
