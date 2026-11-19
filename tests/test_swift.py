"""Swift (DX-X2): the profile's metrics against the same logic in Go and Java, and module imports as edges."""

from __future__ import annotations

from pathlib import Path

from oxn.graph.depgraph import build_dependency_graph
from oxn.graph.sources import iter_source_files
from oxn.languages import get_parser
from oxn.metrics import cognitive_complexity
from oxn.profiles import get_profile, profile_for_path


def score(language: str, source: str, name: str = "f") -> int:
    profile = get_profile(language)
    root = get_parser(language).parse(source.encode()).root_node
    fn = profile.unwrap(root.named_children[-1])
    return cognitive_complexity(fn, profile, function_name=name).score


SWIFT_ELSE_IF = """
func f(x: Int) -> Int {
  if x > 0 {
    return 1
  } else if x < 0 {
    if x < -10 { return -2 }
    return -1
  } else {
    return 0
  }
}
"""

GO_ELSE_IF = """
package p

func f(x int) int {
  if x > 0 {
    return 1
  } else if x < 0 {
    if x < -10 { return -2 }
    return -1
  } else {
    return 0
  }
}
"""


def test_an_else_if_chain_scores_as_in_go() -> None:
    """`if` +1, `else if` +1, the nested `if` +2 (nesting 1), `else` +1: the same 5 in both grammars."""
    assert score("go", GO_ELSE_IF.strip()) == 5
    assert score("swift", SWIFT_ELSE_IF.strip()) == 5


def test_guard_loops_and_switch_are_structural() -> None:
    source = """
func f(xs: [Int]) -> Int {
  guard !xs.isEmpty else { return 0 }
  for x in xs {
    switch x { case 1: break; default: break }
  }
  while false { }
  return 1
}
"""
    # guard +1, for +1, switch +2 (nested in for), while +1.
    assert score("swift", source.strip()) == 5


def test_boolean_operators_count() -> None:
    assert score("swift", "func f(a: Bool, b: Bool) -> Bool { return a && b }") == 1
    assert score("swift", "func f(a: Bool, b: Bool, c: Bool) -> Bool { return a && b || c }") == 2


def test_swift_files_are_supported() -> None:
    assert profile_for_path("App/Views/Main.swift") is get_profile("swift")


def swift_tree(root: Path) -> Path:
    """A SwiftPM package with two targets, and an app outside it that imports the first."""
    core = root / "Packages" / "Core" / "Sources" / "Core"
    util = root / "Packages" / "Core" / "Sources" / "Util"
    app = root / "App"
    for folder in (core, util, app):
        folder.mkdir(parents=True)
    (core / "Model.swift").write_text("import Foundation\nimport Util\n\npublic struct Model { public init() {} }\n")
    (core / "Store.swift").write_text("public final class Store { public init() {} }\n")
    (util / "Clock.swift").write_text("public enum Clock { public static func now() -> Int { 0 } }\n")
    (app / "Main.swift").write_text("import SwiftUI\n@testable import Core\n\nlet store = Store()\n")
    return root


def test_a_module_import_reaches_every_file_of_its_target(tmp_path: Path) -> None:
    root = swift_tree(tmp_path)
    files = list(iter_source_files([root], base=root))
    graph = build_dependency_graph(root, files, component_of=lambda path: path)
    assert graph.files["App/Main.swift"] == {
        "Packages/Core/Sources/Core/Model.swift",
        "Packages/Core/Sources/Core/Store.swift",
    }
    assert graph.files["Packages/Core/Sources/Core/Model.swift"] == {"Packages/Core/Sources/Util/Clock.swift"}
    assert graph.unresolved == [], "Foundation and SwiftUI are external, not unresolved"
