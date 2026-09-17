def test_typescript_indexes_the_root_configs_and_not_nested_ones(tmp_path) -> None:
    """Root-level and sorted. Walking the tree would index every example app as a project.

    `typescript-nest` carries a `tsconfig.json` under `packages/` and one in each of 404
    `sample/` applications; treating those as projects indexes the samples as repositories in
    their own right, which is not what the tree declares itself to be.
    """
    from oxn.scip.runner import INDEXERS

    (tmp_path / "tsconfig.spec.json").write_text("{}")
    (tmp_path / "tsconfig.json").write_text("{}")
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "tsconfig.json").write_text("{}")

    assert INDEXERS["typescript"].root_projects(tmp_path) == ["tsconfig.json", "tsconfig.spec.json"]


def test_an_indexer_with_no_project_glob_appends_nothing(tmp_path) -> None:
    """Every other language is unchanged: `scip-python` takes no positional projects."""
    from oxn.scip.runner import INDEXERS

    (tmp_path / "tsconfig.json").write_text("{}")
    assert INDEXERS["python"].root_projects(tmp_path) == []
