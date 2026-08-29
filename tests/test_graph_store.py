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
