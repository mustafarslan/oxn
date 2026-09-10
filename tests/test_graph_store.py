"""The cache contract.

The store is a *cache*: everything in it is derivable from the working tree, so a mismatch
rebuilds rather than migrates. What must never happen is serving a stale result, and the
classic way that happens is forgetting part of the cache key. Each component of the key
gets its own test.
"""

from __future__ import annotations

from oxn.graph.model import EntityKind
from oxn.graph.store import SCHEMA_VERSION, GraphStore

GRAMMAR = "1.15.8"


def test_roundtrip_preserves_entities(tmp_path, build) -> None:
    parsed = build("python", "class C:\n    def m(self, a):\n        pass\n")
    with GraphStore(tmp_path / "g.db") as store:
        store.put_file(parsed, profile_version=1, grammar_version=GRAMMAR)
        restored = store.entities_for(parsed.path)

    assert [e.qualified_name for e in restored] == [e.qualified_name for e in parsed.entities]
    method = next(e for e in restored if e.kind is EntityKind.METHOD)
    assert method.attrs["parameters"] == ["self", "a"]


def test_cache_hit_requires_matching_content(tmp_path, build) -> None:
    parsed = build("python", "def f(): pass\n")
    with GraphStore(tmp_path / "g.db") as store:
        store.put_file(parsed, 1, GRAMMAR)
        assert store.is_current(parsed.path, parsed.content_sha, 1, GRAMMAR)
        assert not store.is_current(parsed.path, "different-sha", 1, GRAMMAR)


def test_cache_is_invalidated_by_profile_version(tmp_path, build) -> None:
    """The classic failure: a profile fix ships and users keep results from the old tables."""
    parsed = build("python", "def f(): pass\n")
    with GraphStore(tmp_path / "g.db") as store:
        store.put_file(parsed, profile_version=1, grammar_version=GRAMMAR)
        assert not store.is_current(parsed.path, parsed.content_sha, 2, GRAMMAR)


def test_cache_is_invalidated_by_grammar_version(tmp_path, build) -> None:
    parsed = build("python", "def f(): pass\n")
    with GraphStore(tmp_path / "g.db") as store:
        store.put_file(parsed, 1, GRAMMAR)
        assert not store.is_current(parsed.path, parsed.content_sha, 1, "9.9.9")


def test_reindex_replaces_rather_than_accumulates(tmp_path, build) -> None:
    db = tmp_path / "g.db"
    with GraphStore(db) as store:
        store.put_file(build("python", "def f(): pass\ndef g(): pass\n"), 1, GRAMMAR)
        assert store.stats()["entities"] == 3
        store.put_file(build("python", "def f(): pass\n"), 1, GRAMMAR)
        assert store.stats()["entities"] == 2, "stale entities survived a re-index"


def test_forget_removes_a_file_entirely(tmp_path, build) -> None:
    parsed = build("python", "def f(): pass\n")
    with GraphStore(tmp_path / "g.db") as store:
        store.put_file(parsed, 1, GRAMMAR)
        store.forget(parsed.path)
        assert store.entities_for(parsed.path) == []
        assert store.known_paths() == set()


def test_schema_version_mismatch_rebuilds(tmp_path, build) -> None:
    db = tmp_path / "g.db"
    with GraphStore(db) as store:
        store.put_file(build("python", "def f(): pass\n"), 1, GRAMMAR)
        assert store.stats()["files"] == 1

    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (SCHEMA_VERSION + 1,))
    conn.commit()
    conn.close()

    with GraphStore(db) as store:
        assert store.stats()["files"] == 0, "a schema mismatch must rebuild, not serve stale rows"


def test_store_creates_its_parent_directory(tmp_path) -> None:
    with GraphStore(tmp_path / "deep" / "nested" / "g.db") as store:
        assert store.stats()["schema_version"] == SCHEMA_VERSION


# ---- a file row is not a measurement ---------------------------------------------------------


def test_a_parsed_but_unmeasured_file_is_not_current(tmp_path) -> None:
    """The freshness key answered "has this been parsed" and was asked "has this been seen".

    `scip.ingest` writes a file row directly -- it needs one before symbols and edges can
    reference it -- and `put_file` is a *replace*, so it takes that file's metrics with it.
    The row it leaves carries the current content sha, so the next `index_file` saw a current
    file and returned early. Nothing measured it again.

    **Measured on `python-httpx` on 2026-09-10: after `oxn index --index-file`, the metrics
    table held 0 rows, and `oxn check .` reported "60 files, 0 violations, passed" on a
    corpus that has 118.** A gate that cannot see renders exactly like a gate that found
    nothing, which is the most dangerous shape a defect in this project can take.
    """
    from oxn.graph.builder import build_file, content_sha
    from oxn.graph.indexer import Indexer
    from oxn.languages import get_parser
    from oxn.profiles import get_profile

    source = b"def f(x):\n    return x\n"
    (tmp_path / "m.py").write_bytes(source)
    profile = get_profile("python")

    with Indexer(root=tmp_path, cache_path=tmp_path / "graph.db") as indexer:
        indexer.index()
        stamp = (content_sha(source), profile.version, indexer.grammar_version())
        assert indexer.store.is_current("m.py", *stamp, measured=True)

        # What the ingest path does: a file row, and nothing else.
        tree = get_parser("python").parse(source)
        parsed = build_file("m.py", source, profile, tree.root_node)
        indexer.store.put_file(parsed, profile.version, indexer.grammar_version())

        assert indexer.store.is_current("m.py", *stamp), "the parse really is current"
        assert not indexer.store.is_current("m.py", *stamp, measured=True), (
            "and the measurement really is gone"
        )

        # And the indexer must act on the difference rather than trust the row.
        indexer.index()
        assert indexer.store.measurements_for("m.py"), "re-measured rather than skipped"


def test_ingesting_an_index_does_not_throw_away_what_was_measured(tmp_path) -> None:
    """The other half: the guard above makes the loss visible, this stops it happening.

    Left to clobber, `oxn index` deleted every metric row, `oxn check` re-parsed every file
    to restore them, and that in turn deleted every call edge the ingest had just written --
    the two commands undoing each other in a loop. Driven through `ingest_index` over a
    synthetic index rather than asserted on the guard, because the guard is not the promise.
    """
    from tests.test_scip import delimited, document, occurrence

    from oxn.graph.indexer import Indexer
    from oxn.scip.ingest import ingest_index

    source = b"def helper():\n    return 1\n"
    (tmp_path / "m.py").write_bytes(source)
    # `helper` is defined at line 0, columns 4..10; role 1 is a definition.
    index = tmp_path / "one.scip"
    index.write_bytes(
        delimited(2, document("m.py", [occurrence("scip py p 1 `m`/helper().", [0, 4, 10], 1)]))
    )

    with Indexer(root=tmp_path, cache_path=tmp_path / "graph.db") as indexer:
        indexer.index()
        before = dict(indexer.store.measurements_for("m.py"))
        assert before, "the fixture has to have been measured for this to prove anything"

        report = ingest_index(indexer, index)
        assert report.matched_documents == 1, "the index has to have been read"
        assert dict(indexer.store.measurements_for("m.py")) == before


def test_measuring_an_ingested_file_does_not_throw_away_its_call_edges(tmp_path) -> None:
    """The third half, and the direction the other two left open.

    `test_a_parsed_but_unmeasured_file_is_not_current` makes the indexer revisit a file that
    `scip.ingest` wrote a row for, and revisiting called `put_file` -- a delete-and-insert
    the schema cascades from -- which took that file's SCIP symbols and edges with it. So
    `oxn index` wrote the graph and the very next `oxn check` destroyed it: 4,176 call edges
    on `python-httpx`, gone, with `oxn calls` answering UNAVAILABLE on a tree just indexed.

    Parsed, measured and ingested are three facts about one row, and only re-parsing may
    replace it. Asserted on the edges rather than on the freshness call, because the guard
    is not the promise.
    """
    from tests.test_scip import delimited, document, occurrence

    from oxn.graph.indexer import Indexer
    from oxn.scip.ingest import ingest_index

    (tmp_path / "m.py").write_bytes(
        b"def helper():\n    return 1\n\n\ndef main():\n    return helper()\n"
    )
    index = tmp_path / "one.scip"
    index.write_bytes(
        delimited(
            2,
            document(
                "m.py",
                [
                    occurrence("scip py p 1 `m`/helper().", [0, 4, 10], 1),
                    occurrence("scip py p 1 `m`/main().", [4, 4, 8], 1),
                    occurrence("scip py p 1 `m`/helper().", [5, 11, 17]),
                ],
            ),
        )
    )

    # An ingest with no measuring pass, which is what `oxn index` does.
    with Indexer(root=tmp_path, cache_path=tmp_path / "graph.db", measure=False) as indexer:
        indexer.index()
        ingest_index(indexer, index)
        edges = indexer.store.edge_stats()
        assert edges.get("calls_resolved"), f"the ingest must have written an edge, got {edges}"

    # ... and then any measuring command, which must not undo it.
    with Indexer(root=tmp_path, cache_path=tmp_path / "graph.db") as indexer:
        indexer.index()
        assert dict(indexer.store.measurements_for("m.py")), "the file must now be measured"
        assert indexer.store.edge_stats() == edges, "and its call graph must have survived"
