"""A SCIP `local` symbol is document-scoped, and OXN's key for it must be too.

**This is the defect that made 17% of nest's L2 edges point at the wrong file.** The
numbering in `local 0`, `local 1`, ... restarts in every document -- `typescript-nest`'s
index has 628 documents that each define `local 0` -- but `symbols.symbol` is a PRIMARY KEY
and `resolve_edge_targets` joins on it with no file condition. So the last file ingested won
`local 0`, and every other file's reference to its own `local 0` resolved into that file.

529 fabricated `calls` edges on nest, with `packages/common` calling `packages/platform-
express`. L2 is the rung L0 and L1 are *graded against*, so a wrong edge here is a wrong
fact in `metrics.callgraph`, CBO, RFC and dead code alike.

The real corpora check is in `test_oracle_scip.py`, which asserts zero cross-file local
resolutions on nest. These run everywhere and pin the rule itself.
"""

from __future__ import annotations

from oxn.graph.builder import build_file
from oxn.graph.indexer import Indexer
from oxn.languages import get_parser
from oxn.profiles import get_profile
from oxn.scip.index import ScipDocument, ScipOccurrence
from oxn.scip.join import join_document, scoped

SOURCE = """
def helper(value):
    return value


def caller(value):
    return helper(value)
"""


def test_a_local_symbol_is_qualified_and_a_global_one_is_untouched() -> None:
    """The rule, stated once. A global symbol must keep its exact spelling: it is the join
    key across files, and qualifying it would break every cross-file edge there is."""
    assert scoped("local 0", "a.py") == "local 0 a.py"
    assert scoped("scip-python python p 1 `m`/helper().", "a.py") == (
        "scip-python python p 1 `m`/helper()."
    )


def _joined(path: str, symbol: str):
    """Join `SOURCE` under `path`, with both the definition and the call site on `symbol`."""
    profile = get_profile("python")
    data = SOURCE.encode()
    tree = get_parser(profile.grammar).parse(data)
    entities = list(build_file(path, data, profile, tree.root_node).entities)
    document = ScipDocument(
        relative_path=path,
        occurrences=[
            # `def helper` on line 1, and the call to it on line 6.
            ScipOccurrence(symbol, 1, 4, 1, 10, 1),
            ScipOccurrence(symbol, 6, 11, 6, 17, 0),
        ],
    )
    return entities, join_document(entities, profile, tree.root_node, document)


def test_two_files_that_both_define_local_0_do_not_resolve_into_each_other(tmp_path) -> None:
    """The whole bug in one test. Two documents, the same `local 0`, and each call must
    reach its own file's definition -- which a bare-symbol key cannot express, because the
    second file's row replaces the first's."""
    root = tmp_path / "tree"
    root.mkdir()
    for name in ("a.py", "b.py"):
        (root / name).write_text(SOURCE)

    with Indexer(root=root, cache_path=tmp_path / "graph.db") as indexer:
        expected = {}
        for name in ("a.py", "b.py"):
            entities, result = _joined(name, "local 0")
            parsed = build_file(
                name,
                SOURCE.encode(),
                get_profile("python"),
                get_parser("python").parse(SOURCE.encode()).root_node,
            )
            indexer.store.put_file(parsed, get_profile("python").version, indexer.grammar_version())
            indexer.store.put_symbols(name, result.definitions)
            indexer.store.put_edges(name, result.edges)
            expected[name] = next(e.id for e in entities if e.name == "helper")
        indexer.store.resolve_edge_targets()

        rows = indexer.store._conn.execute(  # noqa: SLF001 - a defect needs the rows
            "SELECT file_path, dst_ref, dst_id FROM edges WHERE dst_ref IS NOT NULL"
        ).fetchall()

    assert rows, "the join produced no call edge at all"
    for row in rows:
        assert row["dst_id"] == expected[row["file_path"]], (
            f"{row['file_path']}'s `local 0` resolved to another file's entity"
        )


def test_the_stored_reference_carries_the_file_that_owns_it() -> None:
    """Both sides of the join are qualified, or neither resolves. Asserting the *shape*
    keeps a future change from qualifying the definition and forgetting the reference."""
    _entities, result = _joined("pkg/a.py", "local 7")

    assert list(result.definitions) == ["local 7 pkg/a.py"]
    assert [edge.dst_ref for edge in result.edges] == ["local 7 pkg/a.py"]
