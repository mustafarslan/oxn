"""Calls made through an imported name, which SCIP hands back as a document-local.

`scip-python` binds `from m import helper` as `local 2` and resolves the call site to that
same `local 2`. A local is document-scoped by the specification, so the edge names a symbol
nothing in the tree defines -- and the index carries no way to recover it: both occurrences
are plain reads, with no definition role and no relationships tying the local to what it
aliases.

The recovery is OXN's own L1 resolver, and the interesting tests here are the ones about what
it *refuses*. A local is not always an import binding, and answering one that is not is how a
fabricated edge gets into the cache wearing the same clothes as a real one.
"""

from __future__ import annotations

import pytest
from tests.test_scip import delimited, document, occurrence

from oxn.graph.indexer import Indexer
from oxn.scip.ingest import ingest_index

#: A `local N` occurrence, as scip-python writes one for an imported name: role 8, a plain
#: read, at the position of the name itself.
READ = 8


def _index(tmp_path, documents) -> object:
    path = tmp_path / "one.scip"
    path.write_bytes(b"".join(delimited(2, doc) for doc in documents))
    return path


def _ingest(tmp_path):
    with Indexer(root=tmp_path, cache_path=tmp_path / "graph.db", measure=False) as indexer:
        indexer.index()
        report = ingest_index(indexer, tmp_path / "one.scip")
        edges = [
            dict(row)
            for row in indexer.store._conn.execute(  # noqa: SLF001
                "SELECT e.dst_id, e.resolution, e.confidence, t.qualified_name AS target"
                " FROM edges e LEFT JOIN entities t ON t.id = e.dst_id"
                " WHERE e.kind = 'calls'"
            )
        ]
    return report, edges


def _target_module(tmp_path, body: bytes = b"def helper():\n    return 1\n") -> None:
    (tmp_path / "m.py").write_bytes(body)


def test_a_call_through_an_imported_name_resolves_at_l1(tmp_path) -> None:
    """The gap itself: 990 such edges on OXN's own tree, 788 on `typescript-nest`.

    The rung is the point. SCIP found the *call* -- that is what `provenance` keeps -- and a
    name resolver found what it calls, which is L1 and must say so, because
    `docs/metrics.md` section 2.3 promises exactness per rung and a row that kept L2 would be
    claiming compiler-grade evidence for a name-based answer.
    """
    _target_module(tmp_path)
    (tmp_path / "u.py").write_bytes(b"from m import helper\n\n\ndef use():\n    return helper()\n")
    _index(
        tmp_path,
        [
            document("m.py", [occurrence("scip py p 1 `m`/helper().", [0, 4, 10], 1)]),
            document(
                "u.py",
                [
                    occurrence("scip py p 1 `u`/use().", [3, 4, 7], 1),
                    occurrence("local 0", [0, 16, 22], READ),
                    occurrence("local 0", [4, 11, 17], READ),
                ],
            ),
        ],
    )
    report, edges = _ingest(tmp_path)

    assert report.aliases.resolved == 1, report.aliases.as_dict()
    resolved = [edge for edge in edges if edge["dst_id"]]
    assert len(resolved) == 1, edges
    assert resolved[0]["target"] == "m.helper"
    assert resolved[0]["resolution"] == "L1", "SCIP found the call; L1 found the callee"
    assert resolved[0]["confidence"] == 1.0


def test_a_local_holding_a_callable_is_not_an_import_and_stays_unresolved(tmp_path) -> None:
    """**The gate that keeps this from fabricating edges.**

    `handler = lambda: 1` then `handler()` also resolves to a document-local, and the L1
    resolver would happily answer it from "a unique declaration anywhere in the project" --
    reaching the unrelated `handler` in `m.py`. That is the shape `scip.join.scoped` records,
    where a local was allowed to mean something outside its own document. Measured over OXN's
    2,197 local-target calls, 0.5% are this; the cost of getting one wrong is a call edge
    between two files that never mention each other.
    """
    _target_module(tmp_path, b"def handler():\n    return 1\n")
    (tmp_path / "u.py").write_bytes(b"def use():\n    handler = lambda: 1\n    return handler()\n")
    _index(
        tmp_path,
        [
            document("m.py", [occurrence("scip py p 1 `m`/handler().", [0, 4, 11], 1)]),
            document(
                "u.py",
                [
                    occurrence("scip py p 1 `u`/use().", [0, 4, 7], 1),
                    occurrence("local 0", [1, 4, 11], READ),
                    occurrence("local 0", [2, 11, 18], READ),
                ],
            ),
        ],
    )
    report, edges = _ingest(tmp_path)

    assert report.aliases.resolved == 0, "a local variable is not an import binding"
    assert report.aliases.not_an_import == 1, report.aliases.as_dict()
    assert [edge for edge in edges if edge["dst_id"]] == [], edges


def test_an_imported_name_reassigned_in_the_file_stays_unresolved(tmp_path) -> None:
    """`from m import f` then `f = wrap(f)`: the call may reach the wrapper, not the import.

    `Scope.declare` keeps the *first* binding, so the import on line 1 hides the assignment
    and a lookup alone answers "import" for both. The file's assigned names are collected
    separately for exactly this. It fired 0 times over OXN's 990 candidates -- which is a
    reason to keep it cheap, not a reason to leave it out.
    """
    _target_module(tmp_path, b"def f():\n    return 1\n")
    (tmp_path / "u.py").write_bytes(
        b"from m import f\n\nf = wrap(f)\n\n\ndef use():\n    return f()\n"
    )
    _index(
        tmp_path,
        [
            document("m.py", [occurrence("scip py p 1 `m`/f().", [0, 4, 5], 1)]),
            document(
                "u.py",
                [
                    occurrence("scip py p 1 `u`/use().", [5, 4, 7], 1),
                    occurrence("local 0", [0, 14, 15], READ),
                    occurrence("local 0", [6, 11, 12], READ),
                ],
            ),
        ],
    )
    report, edges = _ingest(tmp_path)

    assert report.aliases.rebound == 1, report.aliases.as_dict()
    assert [edge for edge in edges if edge["dst_id"]] == [], edges


def test_an_ambiguous_name_is_counted_and_not_written(tmp_path) -> None:
    """Two declarations of one name is not an answer, and must not be stored as one.

    `ResolvedName` carries `1/n` so an ambiguous answer *can* be recorded honestly, and
    `build_call_graph` declines anything below L2 that is not certain. It is still not
    written: a row with `dst_id` set is fact-shaped, and 468 of these on `typescript-nest`
    would be 468 probably-wrong targets waiting for a reader who forgets to check.
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_bytes(b"")
    (tmp_path / "pkg" / "a.py").write_bytes(
        b"def helper():\n    return 1\n\n\ndef helper():\n    return 2\n"
    )
    (tmp_path / "u.py").write_bytes(
        b"from pkg.a import helper\n\n\ndef use():\n    return helper()\n"
    )
    _index(
        tmp_path,
        [
            document("pkg/a.py", [occurrence("scip py p 1 `pkg.a`/helper().", [0, 4, 10], 1)]),
            document(
                "u.py",
                [
                    occurrence("scip py p 1 `u`/use().", [3, 4, 7], 1),
                    occurrence("local 0", [0, 18, 24], READ),
                    occurrence("local 0", [4, 11, 17], READ),
                ],
            ),
        ],
    )
    report, edges = _ingest(tmp_path)

    assert report.aliases.resolved == 0, report.aliases.as_dict()
    assert report.aliases.ambiguous == 1, report.aliases.as_dict()
    assert [edge for edge in edges if edge["dst_id"]] == [], edges


@pytest.mark.parametrize("name", ["isinstance", "len"])
def test_a_builtin_is_not_an_import(tmp_path, name: str) -> None:
    """The largest group by far, and it must cost nothing.

    408 of `python-httpx`'s 4,073 call edges resolve to a document-local, and the top of that
    list is `isinstance` (159) and `len` (108). They are unresolvable because they are not in
    the tree, which is the correct answer and not a gap.
    """
    (tmp_path / "u.py").write_bytes(f"def use(x):\n    return {name}(x)\n".encode())
    _index(
        tmp_path,
        [
            document(
                "u.py",
                [
                    occurrence("scip py p 1 `u`/use().", [0, 4, 7], 1),
                    occurrence("local 0", [1, 11, 11 + len(name)], READ),
                ],
            )
        ],
    )
    report, _ = _ingest(tmp_path)

    assert report.aliases.resolved == 0
    assert report.aliases.not_an_import == 1, report.aliases.as_dict()
