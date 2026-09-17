"""How each language's SCIP indexer is invoked.

`Indexer.argv` is data rather than a branch, and `Indexer.projects` is the part of that data
which decides *how much of a tree* gets indexed at all -- the difference between covering
1,020 of `typescript-nest`'s files and 1,444 of them. These assert the discovery rule, which
is the thing that would quietly revert.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from oxn.scip import runner
from oxn.scip.runner import INDEXERS, Indexer, IndexerTimeout, run_indexer


def test_typescript_indexes_the_root_configs_and_not_nested_ones(tmp_path: Path) -> None:
    """Root-level and sorted. Walking the tree would index every example app as a project.

    `typescript-nest` carries a `tsconfig.json` under `packages/` and one in each of 404
    `sample/` applications; treating those as projects indexes the samples as repositories in
    their own right, which is not what the tree declares itself to be.
    """
    (tmp_path / "tsconfig.spec.json").write_text("{}")
    (tmp_path / "tsconfig.json").write_text("{}")
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "tsconfig.json").write_text("{}")

    assert INDEXERS["typescript"].root_projects(tmp_path) == ["tsconfig.json", "tsconfig.spec.json"]


def test_an_indexer_with_no_project_glob_appends_nothing(tmp_path: Path) -> None:
    """Every other language is unchanged: `scip-python` takes no positional projects."""
    (tmp_path / "tsconfig.json").write_text("{}")
    assert INDEXERS["python"].root_projects(tmp_path) == []


def test_javascript_indexes_the_root_configs_too(tmp_path: Path) -> None:
    """eslint carries `tsconfig.base.json` with `checkJs`, and OXN was ignoring it.

    Every JavaScript figure published before 2026-09-17 was measured against a one-line
    `{"allowJs": true}` that `--infer-tsconfig` had written itself. Reading the project's own
    configuration instead takes call coverage 47.0% -> 52.1%, and `lib/` 51.9% -> 59.9%.
    """
    (tmp_path / "tsconfig.types.json").write_text("{}")
    (tmp_path / "tsconfig.base.json").write_text("{}")

    assert INDEXERS["javascript"].root_projects(tmp_path) == [
        "tsconfig.base.json",
        "tsconfig.types.json",
    ]


def test_an_inferred_config_is_removed_again(tmp_path: Path) -> None:
    """A tool that measures a repository may not modify it.

    `--infer-tsconfig` writes a `tsconfig.json` when it finds none. This asserts the removal
    against the registry entry rather than against a live indexer, so it holds without
    `scip-typescript` installed -- the flag and the cleanup are declared together and must not
    drift apart.
    """
    from oxn.scip.runner import _remove_artifacts

    javascript = INDEXERS["javascript"]
    assert javascript.leaves_behind == ("tsconfig.json",)
    assert "--infer-tsconfig" in javascript.template

    (tmp_path / "tsconfig.json").write_text("{}")
    _remove_artifacts(tmp_path, javascript.leaves_behind, existing=set())
    assert not (tmp_path / "tsconfig.json").exists()


def test_a_config_the_project_already_had_survives(tmp_path: Path) -> None:
    """Deleting a file OXN did not create would be worse than leaving one behind."""
    from oxn.scip.runner import _remove_artifacts

    kept = tmp_path / "tsconfig.json"
    kept.write_text('{"compilerOptions": {"strict": true}}')
    _remove_artifacts(tmp_path, ("tsconfig.json",), existing={"tsconfig.json"})

    assert kept.read_text() == '{"compilerOptions": {"strict": true}}'


def _fake(command: str) -> Indexer:
    """A shell 'indexer', so the timeout path can be tested without a corpus or a JDK."""
    return Indexer("fake", "sh", "install nothing", ("-c", command))


def test_an_indexer_that_outruns_its_budget_says_so(tmp_path: Path) -> None:
    """A raw `subprocess.TimeoutExpired` told the user nothing they could act on.

    Java is where this happens: `scip-java` runs Maven, which is 361 s on 4,214 lines and
    scales with the half of the work that is compilation. The message has to name the way
    out, which is the same for every language -- build the index separately.
    """
    runner.INDEXERS["fake"] = _fake("sleep 30")
    try:
        with pytest.raises(IndexerTimeout) as raised:
            run_indexer("fake", tmp_path, tmp_path / "out.scip", timeout=1)
    finally:
        del runner.INDEXERS["fake"]

    assert "--index-file" in str(raised.value)
    assert "1s budget" in str(raised.value)


def test_a_timeout_kills_the_whole_process_tree(tmp_path: Path) -> None:
    """`subprocess.run(timeout=)` kills the child it started, and nothing beneath it.

    An indexer is rarely one process -- `scip-java` is a JVM running Maven running `javac` --
    so a timeout used to leave the build running with nobody waiting for it. Measured on this
    fixture before the fix: two survivors.
    """
    # A sleep duration nothing else on the machine will be using, since `pgrep` is the only
    # way to ask "did anything outlive this". A `#` marker does not work here: `sh` treats the
    # rest of the line as a comment, so the fixture becomes one process and passes either way.
    marker = f"sleep {4700 + os.getpid() % 90}"
    runner.INDEXERS["fake"] = _fake(f"{marker} & {marker}")
    try:
        with pytest.raises(IndexerTimeout):
            run_indexer("fake", tmp_path, tmp_path / "out.scip", timeout=1)
        time.sleep(0.3)
        survivors = subprocess.run(
            ["pgrep", "-f", marker], capture_output=True, text=True, check=False
        ).stdout.split()
    finally:
        subprocess.run(["pkill", "-f", marker], check=False)
        del runner.INDEXERS["fake"]

    assert survivors == [], f"{len(survivors)} process(es) outlived the indexer"


def test_a_timeout_still_cleans_up_what_the_run_created(tmp_path: Path) -> None:
    """The failed run is exactly when an artifact would survive unnoticed."""
    runner.INDEXERS["fake"] = Indexer(
        "fake", "sh", "", ("-c", "touch tsconfig.json; sleep 30"), leaves_behind=("tsconfig.json",)
    )
    try:
        with pytest.raises(IndexerTimeout):
            run_indexer("fake", tmp_path, tmp_path / "out.scip", timeout=1)
    finally:
        del runner.INDEXERS["fake"]

    assert not (tmp_path / "tsconfig.json").exists()


def test_each_language_carries_its_own_budget() -> None:
    """Java's is the outlier and the reason is Maven, not indexing."""
    assert INDEXERS["java"].timeout == 2400
    assert INDEXERS["go"].timeout == 900
