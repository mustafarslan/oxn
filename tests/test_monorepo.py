"""Monorepo resolution: a bare specifier that names one of this repository's own packages.

**Measuring first changed what this had to be.** The roadmap carried "monorepo support" as
an open item on the strength of `resolve.py`'s own docstring, which lists "workspace globs,
symlinked monorepo packages" among the things reported as external rather than resolved. On
`typescript-nest` — a real npm-workspaces monorepo, and the pinned corpus — that turned out
to be false: **all 1,651 cross-package imports already resolved**, because nest declares
every `@nestjs/*` package in `tsconfig.json` `paths` and OXN has read `paths` since P4.

What was genuinely missing is the monorepo that declares `workspaces` and *no* aliases,
where `@scope/pkg` is reachable only through a `node_modules` symlink OXN does not follow.
Every fixture here is that repository, because it is the only one the feature is about.

The second half is classification. `_is_internal_looking` was `raw.is_relative`, so an
unresolvable `@scope/pkg/moved` counted as a third-party dependency — indistinguishable from
`express`, and invisible. `docs/metrics.md` §4.1 requires a missing edge to be *visible*, and
this was the single class of missing edge that looked exactly like correct behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oxn.config import Config
from oxn.graph.depgraph import build_dependency_graph
from oxn.graph.sources import iter_source_files


def workspace(root: Path, declaration: dict | str, *, packages: dict[str, str]) -> Path:
    """A monorepo with no tsconfig at all: `workspaces` is the only thing pointing anywhere."""
    if isinstance(declaration, str):
        (root / "pnpm-workspace.yaml").write_text(declaration)
        (root / "package.json").write_text(json.dumps({"name": "root"}))
    else:
        (root / "package.json").write_text(json.dumps(declaration))
    for name, directory in packages.items():
        target = root / directory
        target.mkdir(parents=True, exist_ok=True)
        (target / "package.json").write_text(json.dumps({"name": name, "main": "./index.js"}))
    return root


def graph_for(root: Path):
    files = list(iter_source_files([root], base=root, exclude=Config.defaults(root).exclude))
    return build_dependency_graph(root, files)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Two packages, one importing the other by name, and nothing else to go on."""
    root = workspace(
        tmp_path,
        {"name": "root", "workspaces": ["packages/*"]},
        packages={"@scope/ui": "packages/ui", "@scope/core": "packages/core"},
    )
    (root / "packages" / "core" / "index.ts").write_text("export const core = 1;\n")
    (root / "packages" / "core" / "deep.ts").write_text("export const deep = 2;\n")
    (root / "packages" / "ui" / "index.ts").write_text(
        "import { core } from '@scope/core';\nexport const ui = core;\n"
    )
    return root


def test_a_cross_package_import_becomes_an_edge(repo: Path) -> None:
    """The gap, stated as the thing it prevents: without this, a layer contract in a
    monorepo cannot see the imports that cross packages -- which are the only ones it is
    about."""
    graph = graph_for(repo)

    assert graph.files["packages/ui/index.ts"] == {"packages/core/index.ts"}


def test_a_subpath_into_a_package_resolves_like_a_relative_import(repo: Path) -> None:
    """`@scope/core/deep.js` must find `deep.ts` -- the TypeScript convention of writing the
    compiled extension. Reusing the relative resolver is what buys that for free."""
    (repo / "packages" / "ui" / "index.ts").write_text(
        "import { deep } from '@scope/core/deep.js';\nexport const ui = deep;\n"
    )
    graph = graph_for(repo)

    assert graph.files["packages/ui/index.ts"] == {"packages/core/deep.ts"}


def test_the_longest_package_name_wins(tmp_path: Path) -> None:
    """`@scope/ui-icons` must not be resolved by a package called `@scope/ui`, which a
    naive prefix match does, silently, and only in repositories that have both."""
    root = workspace(
        tmp_path,
        {"name": "root", "workspaces": ["packages/*"]},
        packages={"@scope/ui": "packages/ui", "@scope/ui-icons": "packages/ui-icons"},
    )
    (root / "packages" / "ui" / "index.ts").write_text("export const ui = 1;\n")
    (root / "packages" / "ui-icons" / "index.ts").write_text("export const icons = 2;\n")
    (root / "app.ts").write_text("import { icons } from '@scope/ui-icons';\n")

    assert graph_for(root).files["app.ts"] == {"packages/ui-icons/index.ts"}


def test_a_workspace_import_that_does_not_resolve_is_reported(repo: Path) -> None:
    """The classification half. A third-party package correctly resolves to nothing; one of
    our own packages resolving to nothing is a missing edge, and the two must not look the
    same in the output."""
    (repo / "packages" / "ui" / "index.ts").write_text(
        "import { gone } from '@scope/core/moved';\nimport express from 'express';\n"
    )
    graph = graph_for(repo)

    reported = {u.specifier for u in graph.unresolved}
    assert reported == {"@scope/core/moved"}, "express is not ours and must stay quiet"


@pytest.mark.parametrize(
    "declaration",
    [
        pytest.param({"name": "root", "workspaces": ["packages/*"]}, id="npm-list"),
        pytest.param(
            {"name": "root", "workspaces": {"packages": ["packages/*"]}}, id="yarn-object"
        ),
        pytest.param("packages:\n  - 'packages/*'\n", id="pnpm-yaml"),
    ],
)
def test_every_way_a_workspace_is_declared(tmp_path: Path, declaration) -> None:
    """Three spellings in the wild, and a repository using one of them is not a special
    case to its own developers."""
    root = workspace(tmp_path, declaration, packages={"@scope/core": "packages/core"})
    (root / "packages" / "core" / "index.ts").write_text("export const core = 1;\n")
    (root / "app.ts").write_text("import { core } from '@scope/core';\n")

    assert graph_for(root).files["app.ts"] == {"packages/core/index.ts"}


def test_the_package_name_is_read_from_the_manifest_not_the_directory(tmp_path: Path) -> None:
    """`packages/common` calls itself `@nestjs/common`, and it is the *name* that appears in
    an import. Deriving one from the other works on exactly the repositories that did not
    need this feature."""
    root = workspace(
        tmp_path, {"name": "root", "workspaces": ["libs/*"]}, packages={"@acme/widgets": "libs/w"}
    )
    (root / "libs" / "w" / "index.ts").write_text("export const w = 1;\n")
    (root / "app.ts").write_text("import { w } from '@acme/widgets';\n")

    assert graph_for(root).files["app.ts"] == {"libs/w/index.ts"}


def test_a_repository_that_is_not_a_monorepo_pays_nothing(tmp_path: Path) -> None:
    """No `workspaces` key means no globbing, no manifest reads, and no behaviour change."""
    (tmp_path / "package.json").write_text(json.dumps({"name": "plain", "version": "1.0.0"}))
    (tmp_path / "app.ts").write_text("import express from 'express';\n")

    graph = graph_for(tmp_path)

    assert graph.files["app.ts"] == set()
    assert graph.unresolved == [], "a third-party import is not a missing edge"


def test_a_malformed_manifest_does_not_fail_the_run(tmp_path: Path) -> None:
    """A broken `package.json` is a thing that happens mid-edit, and a gate that crashes on
    one is a gate that is off exactly when someone is working."""
    (tmp_path / "package.json").write_text('{"name": "root", "workspaces": [')
    (tmp_path / "app.ts").write_text("import x from '@scope/core';\n")

    assert graph_for(tmp_path).files["app.ts"] == set()


def test_tsconfig_paths_win_over_the_workspace_layout(tmp_path: Path) -> None:
    """`paths` is this repository stating where a name resolves; the workspace layout is an
    inference from its directories. A monorepo that declares both and disagrees meant the
    one it wrote down."""
    root = workspace(
        tmp_path,
        {"name": "root", "workspaces": ["packages/*"]},
        packages={"@scope/core": "packages/core"},
    )
    (root / "packages" / "core" / "index.ts").write_text("export const core = 1;\n")
    (root / "vendored").mkdir()
    (root / "vendored" / "index.ts").write_text("export const core = 2;\n")
    (root / "tsconfig.json").write_text(
        json.dumps({"compilerOptions": {"paths": {"@scope/core": ["./vendored"]}}})
    )
    (root / "app.ts").write_text("import { core } from '@scope/core';\n")

    assert graph_for(root).files["app.ts"] == {"vendored/index.ts"}
