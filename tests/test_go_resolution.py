"""Go imports become edges, and the three languages that had none say so when they fail.

**The gate was off.** Go, Rust and Java produced *zero* in-tree dependency edges: every
import resolved to nothing, and `_is_internal_looking` then filed each one as a third-party
package, so nothing appeared in `unresolved` either. go-kit's 1,131 imports, ripgrep's 1,053
and petclinic's 471 were all "correctly external". A layered contract over a Go repository
therefore passed unconditionally -- not because the code obeyed it, but because there were no
edges to judge -- and `test_a_layer_violation_in_go_is_caught` is that, written down.

`docs/metrics.md` section 4.1 is explicit that a missing edge must be *visible*. This is the
same defect the monorepo amendment fixed for a bare `@scope/pkg`, three languages wider.

Go resolves here. Rust and Java are made *visible* and deliberately not resolved: see
`_cargo_crates` and `_java_packages` for what each would additionally need.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oxn.config import Config
from oxn.graph.depgraph import build_dependency_graph
from oxn.graph.resolve import ResolutionContext
from oxn.graph.sources import iter_source_files

GO_MODULE = "module example.com/demo\n\ngo 1.21\n"


def go_tree(root: Path) -> Path:
    """Two packages, each importing the other by module path: a layer violation and a cycle."""
    (root / "go.mod").write_text(GO_MODULE)
    (root / "a").mkdir()
    (root / "b").mkdir()
    (root / "a" / "a.go").write_text(
        'package a\n\nimport "example.com/demo/b"\n\nfunc CallB() string { return b.Name() }\n'
    )
    (root / "b" / "b.go").write_text(
        'package b\n\nimport "example.com/demo/a"\n\n'
        'func Name() string { return "b" }\n'
        "func Back() string { return a.CallB() }\n"
    )
    return root


def graph_for(root: Path):
    files = list(iter_source_files([root], base=root, exclude=Config.defaults(root).exclude))
    return build_dependency_graph(root, files)


def test_a_go_import_of_this_module_becomes_an_edge(tmp_path: Path) -> None:
    """The gap, stated as the thing it prevented: no edge means no contract, no cycle, no
    Martin's metrics, and no `ProjectSymbols.imports` for a whole language."""
    graph = graph_for(go_tree(tmp_path))

    assert graph.files["a/a.go"] == {"b/b.go"}
    assert graph.files["b/b.go"] == {"a/a.go"}


def test_a_layer_violation_in_go_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The demonstration. This exact tree, with this exact contract, reported `passed` --
    and the identical shape in Python reported the violation, which is what made it a
    language gap rather than a fixture mistake."""
    from oxn.check import run_check

    root = go_tree(tmp_path)
    (root / "oxn.yaml").write_text(
        "layers:\n  top: ['a/*']\n  bottom: ['b/*']\n"
        "contracts:\n  - name: top-over-bottom\n    kind: layered\n    order: [top, bottom]\n"
    )
    monkeypatch.chdir(root)

    report = run_check(["b/b.go"], config=Config.load(root))

    reported = {f.key for f in report.findings if f.rule.startswith("contract:")}

    assert reported == {"contract:top-over-bottom|b/b.go|b/b.go -> a/a.go"}


def test_a_go_package_is_a_directory_not_a_file(tmp_path: Path) -> None:
    """Go imports a *package*, and a package is every non-test file in one directory. There
    is no per-file import, so naming one member would pick it arbitrarily."""
    root = tmp_path
    (root / "go.mod").write_text(GO_MODULE)
    (root / "lib").mkdir()
    (root / "lib" / "one.go").write_text("package lib\n\nfunc One() {}\n")
    (root / "lib" / "two.go").write_text("package lib\n\nfunc Two() {}\n")
    (root / "lib" / "one_test.go").write_text("package lib\n\nfunc TestOne() {}\n")
    (root / "app.go").write_text('package main\n\nimport "example.com/demo/lib"\n')

    assert graph_for(root).files["app.go"] == {"lib/one.go", "lib/two.go"}


def test_a_subdirectory_is_a_different_package(tmp_path: Path) -> None:
    """Not recursive, because Go is not: `lib/deep` must be imported separately, and folding
    it in would invent an edge the language does not have."""
    root = tmp_path
    (root / "go.mod").write_text(GO_MODULE)
    (root / "lib" / "deep").mkdir(parents=True)
    (root / "lib" / "top.go").write_text("package lib\n")
    (root / "lib" / "deep" / "deep.go").write_text("package deep\n")
    (root / "app.go").write_text('package main\n\nimport "example.com/demo/lib"\n')

    assert graph_for(root).files["app.go"] == {"lib/top.go"}


def test_a_third_party_import_stays_external(tmp_path: Path) -> None:
    """`startswith` is the obvious wrong way to do this: it makes `example.com/demolition`
    a package of `example.com/demo`."""
    root = tmp_path
    (root / "go.mod").write_text(GO_MODULE)
    (root / "app.go").write_text(
        'package main\n\nimport (\n\t"fmt"\n\t"example.com/demolition/x"\n)\n'
    )

    graph = graph_for(root)

    assert graph.files["app.go"] == set()
    assert graph.unresolved == [], "neither of these is ours, so neither is a missing edge"


def test_an_unplaceable_import_of_our_own_module_is_reported(tmp_path: Path) -> None:
    """The visibility half. A package of ours that resolves to nothing is a *missing edge*;
    `fmt` resolving to nothing is correct behaviour, and the two must not look the same."""
    root = tmp_path
    (root / "go.mod").write_text(GO_MODULE)
    (root / "app.go").write_text(
        'package main\n\nimport (\n\t"fmt"\n\t"example.com/demo/gone"\n)\n'
    )

    reported = {u.specifier for u in graph_for(root).unresolved}

    assert reported == {"example.com/demo/gone"}


def test_a_repository_with_no_go_mod_pays_nothing(tmp_path: Path) -> None:
    (tmp_path / "app.go").write_text('package main\n\nimport "example.com/demo/x"\n')

    assert graph_for(tmp_path).files["app.go"] == set()
    assert graph_for(tmp_path).unresolved == []


# ---- visibility for the two languages that are seen but still not placed -------------------


@pytest.mark.parametrize(
    "specifier",
    [
        # `extract_imports` normalises Rust's `::` to dots, so these arrive dotted. Checking
        # `split("::")` found `super` alone -- 17 of ripgrep's imports rather than its 611.
        "crate.searcher.sink",
        "super.util",
        "self.inner",
    ],
)
def test_a_rust_path_into_this_crate_is_ours(tmp_path: Path, specifier: str) -> None:
    context = ResolutionContext.build(tmp_path, set())

    assert context.is_own_module(specifier)


def test_a_cargo_workspace_member_is_ours_under_the_name_an_import_writes(tmp_path: Path) -> None:
    """A crate declared `grep-searcher` is imported as `grep_searcher`: Rust identifiers hold
    no hyphens. Keying on the declared name matches nothing an import ever says."""
    (tmp_path / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\n')
    (tmp_path / "crates" / "searcher").mkdir(parents=True)
    (tmp_path / "crates" / "searcher" / "Cargo.toml").write_text(
        '[package]\nname = "grep-searcher"\n\n[[test]]\nname = "integration"\n'
    )

    context = ResolutionContext.build(tmp_path, set())

    assert context.cargo_crates == {"grep_searcher": "crates/searcher"}
    assert context.is_own_module("grep_searcher.sinks.UTF8")
    assert not context.is_own_module("regex.Regex")


def test_a_java_import_of_a_package_this_tree_holds_is_ours(tmp_path: Path) -> None:
    """Derived from the layout rather than from `package` lines: Maven and Gradle both put
    `org.example.Thing` under `<source root>/org/example/`, and reading every file to learn
    that costs a parse of the tree on a path budgeted in milliseconds."""
    known = {
        "src/main/java/org/example/model/Owner.java",
        "src/test/java/org/example/OwnerTest.java",
    }
    context = ResolutionContext.build(tmp_path, known)

    assert context.is_own_module("org.example.model.Owner")
    assert context.is_own_module("org.example.OwnerTest")
    assert not context.is_own_module("org.springframework.boot.SpringApplication")


def test_the_manifest_readers_survive_a_broken_file(tmp_path: Path) -> None:
    """A half-written manifest is a thing that happens mid-edit, and a gate that crashes on
    one is a gate that is off exactly when someone is working."""
    (tmp_path / "go.mod").write_text("modu")
    (tmp_path / "Cargo.toml").write_text("[workspace\nmembers = [")

    context = ResolutionContext.build(tmp_path, set())

    assert context.go_module is None
    assert context.cargo_crates == {}
    assert json.dumps(context.java_packages) == "{}"
