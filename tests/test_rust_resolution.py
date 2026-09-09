"""Rust `use` paths become edges, so a layer contract over a Rust crate can see them.

Companion to `test_go_resolution.py`, and the same demonstration: before this, Rust produced
**zero** in-tree dependency edges on `ripgrep` -- 611 imports reported as ours-and-unplaced
after they were made visible, and 1,053 filed as third-party before that -- so a layered
contract over a Rust workspace had nothing to judge and passed unconditionally.

Rust's module tree mirrors its directory tree, so `resolve_rust` probes the known file set
rather than walking `mod` declarations. What that cannot see is listed in the module
docstring and asserted at the bottom of this file, so the limits stay visible rather than
being rediscovered.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oxn.config import Config
from oxn.graph.depgraph import build_dependency_graph
from oxn.graph.sources import iter_source_files

WORKSPACE = '[workspace]\nmembers = ["crates/*"]\n'


def crate(root: Path, name: str, files: dict[str, str], *, manifest: str | None = None) -> None:
    directory = root / "crates" / name
    (directory / "src").mkdir(parents=True, exist_ok=True)
    (directory / "Cargo.toml").write_text(manifest or f'[package]\nname = "{name}"\n')
    for relative, text in files.items():
        target = directory / "src" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def graph_for(root: Path):
    files = list(iter_source_files([root], base=root, exclude=Config.defaults(root).exclude))
    return build_dependency_graph(root, files)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Two crates: `app` reaching into its own modules and across to `engine`."""
    (tmp_path / "Cargo.toml").write_text(WORKSPACE)
    crate(tmp_path, "engine", {"lib.rs": "pub struct Engine;\n"})
    crate(
        tmp_path,
        "app",
        {
            "lib.rs": "mod inner;\nmod deep;\npub struct App;\n",
            "inner.rs": "use crate::App;\nuse engine::Engine;\n",
            "deep/mod.rs": "mod leaf;\n",
            "deep/leaf.rs": "use super::super::App;\nuse crate::inner;\n",
        },
    )
    return tmp_path


def test_a_crate_relative_path_becomes_an_edge(workspace: Path) -> None:
    """`use crate::App` from a submodule reaches its own crate root."""
    assert "crates/app/src/lib.rs" in graph_for(workspace).files["crates/app/src/inner.rs"]


def test_a_cross_crate_path_reaches_the_other_crates_root(workspace: Path) -> None:
    """`use engine::Engine` -- and the crate is named `engine` in a manifest, not by its
    directory."""
    assert "crates/engine/src/lib.rs" in graph_for(workspace).files["crates/app/src/inner.rs"]


def test_super_from_a_file_beside_a_crate_root_reaches_that_root(tmp_path: Path) -> None:
    """The case that was all 54 imports still unresolved on ripgrep once the rest worked.

    A crate root is `lib.rs` or `main.rs` -- never `mod.rs`, and never `src.rs` -- so
    probing only those two spellings found nothing for every file directly beneath one.
    """
    (tmp_path / "Cargo.toml").write_text(WORKSPACE)
    crate(
        tmp_path,
        "one",
        {"lib.rs": "mod sink;\npub struct Sink;\n", "sink.rs": "use super::Sink;\n"},
    )

    assert graph_for(tmp_path).files["crates/one/src/sink.rs"] == {"crates/one/src/lib.rs"}


def test_a_tail_that_names_a_type_falls_back_to_the_module(workspace: Path) -> None:
    """`use crate::inner` and `use crate::inner::Thing` must reach the same file: the tail of
    a path is usually a type, and probing only the full path finds nothing."""
    (workspace / "crates" / "app" / "src" / "deep" / "leaf.rs").write_text(
        "use crate::inner::Thing;\n"
    )

    assert graph_for(workspace).files["crates/app/src/deep/leaf.rs"] == {"crates/app/src/inner.rs"}


def test_a_layer_violation_in_rust_is_caught(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate coming on. This is what zero edges bought: a contract with nothing to judge,
    reporting `passed` on a tree that breaks it."""
    from oxn.check import run_check

    (workspace / "crates" / "engine" / "src" / "lib.rs").write_text(
        "pub struct Engine;\nuse app::App;\n"
    )
    (workspace / "oxn.yaml").write_text(
        "layers:\n  top: ['crates/app/*']\n  bottom: ['crates/engine/*']\n"
        "contracts:\n  - name: top-over-bottom\n    kind: layered\n    order: [top, bottom]\n"
    )
    monkeypatch.chdir(workspace)

    report = run_check(["crates/engine/src/lib.rs"], config=Config.load(workspace))

    assert {f.key for f in report.findings if f.rule.startswith("contract:")} == {
        "contract:top-over-bottom|crates/engine/src/lib.rs|"
        "crates/engine/src/lib.rs -> crates/app/src/lib.rs"
    }


def test_a_dependency_stays_external(workspace: Path) -> None:
    """`std`, `serde`, `bstr` are not ours and must not be reported as missing edges."""
    (workspace / "crates" / "app" / "src" / "inner.rs").write_text(
        "use std::path::Path;\nuse serde::Serialize;\n"
    )

    graph = graph_for(workspace)

    assert graph.files["crates/app/src/inner.rs"] == set()
    assert [u.specifier for u in graph.unresolved] == []


def test_a_crate_root_is_read_from_the_manifest_not_assumed(tmp_path: Path) -> None:
    """ripgrep's own package declares `[[bin]] path = "crates/core/main.rs"`. Assuming
    `src/lib.rs` works for its ten libraries and silently fails for its binary."""
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "top"\n\n[[bin]]\npath = "core/main.rs"\nname = "top"\n'
    )
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "main.rs").write_text("mod util;\n")
    (tmp_path / "core" / "util.rs").write_text("use crate::Thing;\n")

    assert graph_for(tmp_path).files["core/util.rs"] == {"core/main.rs"}
