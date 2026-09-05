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


# ---- `oxn.yaml`'s `exclude`, which was accepted and ignored for four phases --------------


def test_declared_exclusions_are_applied_to_the_walk(repo) -> None:
    found = {p.name for p in iter_source_files([repo], base=repo, exclude=("src/b.py",))}
    assert found == {"a.py", "c.ts"}


def test_a_declared_exclusion_applies_to_a_file_named_directly(repo) -> None:
    """The asymmetry with `DEFAULT_EXCLUDES`, and it is deliberate.

    A named file passes through the directory exclusions -- asking for `node_modules/junk.ts`
    by hand is asking for it. `oxn.yaml`'s `exclude` is a different claim: the project says
    this code is not ours to measure, and that holds however the file is reached. It has to,
    because the `PostToolUse` hook names the one file the agent edited, so an exclusion that
    only applied to directory walks would gate the generated code it was written to exempt.
    """
    named = repo / "src" / "b.py"
    assert list(iter_source_files([named])) == [named]
    assert list(iter_source_files([named], base=repo, exclude=("src/*.py",))) == []


def test_the_indexer_never_parses_what_the_project_excluded(repo, tmp_path) -> None:
    """Filtering the file list after indexing would give the same findings at the same cost.

    The whole point is the cost: excluded code that is parsed and then dropped is as
    expensive as excluded code that is measured.
    """
    indexer = Indexer(root=repo, cache_path=tmp_path / "cache" / "g.db", exclude=("src/*.ts",))
    report = indexer.index([repo])
    indexer.close()
    assert report.parsed == 2, "the TypeScript file was parsed despite being excluded"


def test_a_check_ignores_excluded_files_entirely(repo, tmp_path) -> None:
    """End to end, through the config the way a user writes it."""
    from oxn.check import run_check
    from oxn.config import Config

    (repo / "src" / "generated.py").write_text("def wide(a, b, c, d, e, f, g, h):\n    return a\n")
    over_ceiling = run_check([str(repo / "src")], config=Config.load(repo), use_baseline=False)
    assert any(f.rule == "parameter_count" for f in over_ceiling.findings)

    (repo / "oxn.yaml").write_text("exclude:\n  - 'src/generated.py'\n")
    excluded = run_check([str(repo / "src")], config=Config.load(repo), use_baseline=False)
    assert excluded.findings == []
    assert "src/generated.py" not in excluded.paths


def test_a_path_spelled_with_a_parent_step_gets_the_same_key(repo, tmp_path) -> None:
    """`relative()` produces cache keys, `Finding.path`, and a third of every baseline key,
    so one file must have exactly one spelling.

    `relative_to` is pure text and does not normalize: `src/../src/a.py` stays
    `src/../src/a.py` where `resolve()` gives `src/a.py`. Skipping the syscall for speed is
    fine for the paths a directory walk produces and wrong for a path a person or a foreign
    hook payload can type -- the same file would become two findings, and accepting one
    would not accept the other.
    """
    indexer = Indexer(root=repo, cache_path=tmp_path / "cache" / "g.db")
    direct = indexer.relative(repo / "src" / "a.py")
    detoured = indexer.relative(repo / "src" / ".." / "src" / "a.py")
    indexer.close()

    assert direct == "src/a.py"
    assert detoured == direct


def test_an_exclusion_glob_matches_the_same_path_spelled_either_way(repo, tmp_path) -> None:
    """`_out_of_scope` skips the same syscall under the same guard, and a glob that matched
    one spelling and not the other would exclude a file only when it was named plainly."""
    from oxn.graph.sources import iter_source_files

    detour = repo / "src" / ".." / "src"
    found = iter_source_files([detour], base=repo, exclude=("src/a.py",))

    assert "a.py" not in {path.name for path in found}
