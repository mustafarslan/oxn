"""How each language's SCIP indexer is invoked.

`Indexer.argv` is data rather than a branch, and `Indexer.projects` is the part of that data
which decides *how much of a tree* gets indexed at all -- the difference between covering
1,020 of `typescript-nest`'s files and 1,444 of them. These assert the discovery rule, which
is the thing that would quietly revert.
"""

from __future__ import annotations

from pathlib import Path

from oxn.scip.runner import INDEXERS


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
