"""Incremental indexing over a working tree."""

from __future__ import annotations

from oxn.graph.indexer import Indexer
from oxn.graph.sources import iter_source_files


def _indexer(repo, tmp_path) -> Indexer:
    return Indexer(root=repo, cache_path=tmp_path / "cache" / "g.db")


def test_discovery_skips_excluded_and_unknown_files(repo) -> None:
    found = {p.name for p in iter_source_files([repo])}
    assert found == {"a.py", "b.py", "c.ts"}
    assert "junk.ts" not in found, "node_modules must be excluded"
    assert "README.md" not in found


def test_first_run_parses_second_run_is_all_cache(repo, tmp_path) -> None:
    with _indexer(repo, tmp_path) as indexer:
        first = indexer.index()
        assert (first.parsed, first.cached) == (3, 0)

        second = indexer.index()
        assert (second.parsed, second.cached) == (0, 3)
        assert second.cache_hit_ratio == 1.0


def test_editing_one_file_reparses_only_that_file(repo, tmp_path) -> None:
    with _indexer(repo, tmp_path) as indexer:
        indexer.index()
        (repo / "src" / "a.py").write_text("def f(x):\n    return x + 1\n")
        report = indexer.index()
        assert (report.parsed, report.cached) == (1, 2)


def test_force_reparses_everything(repo, tmp_path) -> None:
    with _indexer(repo, tmp_path) as indexer:
        indexer.index()
        report = indexer.index(force=True)
        assert (report.parsed, report.cached) == (3, 0)


def test_deleted_files_are_pruned_from_the_cache(repo, tmp_path) -> None:
    with _indexer(repo, tmp_path) as indexer:
        indexer.index()
        assert "src/b.py" in indexer.store.known_paths()
        (repo / "src" / "b.py").unlink()
        indexer.index()
        assert "src/b.py" not in indexer.store.known_paths()


def test_scoped_index_does_not_prune_unvisited_files(repo, tmp_path) -> None:
    """A single-file hook run must never evict the rest of the repo's graph."""
    with _indexer(repo, tmp_path) as indexer:
        indexer.index()
        indexer.index([repo / "src" / "a.py"])
        assert indexer.store.known_paths() == {"src/a.py", "src/b.py", "src/c.ts"}


def test_paths_are_repo_relative_and_posix(repo, tmp_path) -> None:
    with _indexer(repo, tmp_path) as indexer:
        indexer.index()
        assert all(not p.startswith("/") for p in indexer.store.known_paths())
        assert "src/a.py" in indexer.store.known_paths()


def test_polyglot_tree_indexes_both_languages(repo, tmp_path) -> None:
    with _indexer(repo, tmp_path) as indexer:
        indexer.index()
        assert indexer.store.entities_for("src/c.ts"), "TypeScript file produced no entities"
        assert indexer.store.entities_for("src/a.py")
