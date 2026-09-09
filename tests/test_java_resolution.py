"""Java imports become edges, so a layer contract over a Java tree can see them.

The third of these files, after Go and Rust, and the same demonstration: before this, Java
produced **zero** in-tree dependency edges -- petclinic's 24 own imports were visible as
ours-and-unplaced, and its other 447 filed as third-party -- so a layered contract had
nothing to judge and passed unconditionally.

**What an import graph cannot see in Java, and it is worth knowing before reading a
number.** A class using another class *in its own package* writes no import at all, so those
edges do not exist here and never will: petclinic's 50 files yield 24 edges, not because the
resolution is weak but because Java only names what crosses a package boundary. That happens
to be the boundary layer contracts are usually drawn on, which is why this is worth having
anyway -- but a coupling metric read off these edges is a *lower bound* and says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oxn.config import Config
from oxn.graph.depgraph import build_dependency_graph
from oxn.graph.sources import iter_source_files

MAIN = "src/main/java/com/acme"
TEST = "src/test/java/com/acme"


def write(root: Path, relative: str, text: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def graph_for(root: Path):
    files = list(iter_source_files([root], base=root, exclude=Config.defaults(root).exclude))
    return build_dependency_graph(root, files)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    write(tmp_path, f"{MAIN}/model/Owner.java", "package com.acme.model;\npublic class Owner {}\n")
    write(
        tmp_path,
        f"{MAIN}/web/OwnerController.java",
        "package com.acme.web;\n\nimport com.acme.model.Owner;\n"
        "\npublic class OwnerController {}\n",
    )
    return tmp_path


def test_an_import_of_this_tree_becomes_an_edge(tree: Path) -> None:
    assert graph_for(tree).files[f"{MAIN}/web/OwnerController.java"] == {f"{MAIN}/model/Owner.java"}


def test_a_third_party_import_stays_external(tree: Path) -> None:
    write(
        tree,
        f"{MAIN}/web/OwnerController.java",
        "package com.acme.web;\n\nimport org.springframework.stereotype.Controller;\n"
        "\npublic class OwnerController {}\n",
    )
    graph = graph_for(tree)

    assert graph.files[f"{MAIN}/web/OwnerController.java"] == set()
    assert graph.unresolved == [], "Spring is not ours, so it is not a missing edge"


def test_the_two_source_roots_are_not_confused(tree: Path) -> None:
    """`src/main/java` and `src/test/java` hold the **same packages** -- 5 of petclinic's 6
    are in both -- and the package table mapped each to a single directory until this.

    A `dict[str, str]` keeps whichever was walked last, which cost nothing while the table
    only answered "is this ours" and would have put half of every resolved import in the
    wrong source root.
    """
    write(tree, f"{TEST}/model/OwnerTests.java", "package com.acme.model;\nclass OwnerTests {}\n")
    write(
        tree,
        f"{TEST}/web/ControllerTests.java",
        "package com.acme.web;\n\nimport com.acme.model.OwnerTests;\n\nclass ControllerTests {}\n",
    )
    graph = graph_for(tree)

    assert graph.files[f"{TEST}/web/ControllerTests.java"] == {f"{TEST}/model/OwnerTests.java"}
    assert graph.files[f"{MAIN}/web/OwnerController.java"] == {f"{MAIN}/model/Owner.java"}


def test_a_static_import_reaches_the_type_that_holds_the_member(tree: Path) -> None:
    """`import static com.acme.model.Owner.of` names a member; the *file* is `Owner.java`,
    and `extract_imports` has already dropped `of` by the time this resolves."""
    write(
        tree,
        f"{MAIN}/web/OwnerController.java",
        "package com.acme.web;\n\nimport static com.acme.model.Owner.of;\n"
        "\npublic class OwnerController {}\n",
    )

    assert graph_for(tree).files[f"{MAIN}/web/OwnerController.java"] == {f"{MAIN}/model/Owner.java"}


def test_a_wildcard_import_reaches_the_whole_package(tree: Path) -> None:
    """`import com.acme.model.*` couples the importer to the package rather than to a member
    it never named -- the same reading `_resolve_go` gives `import "pkg"`."""
    write(tree, f"{MAIN}/model/Pet.java", "package com.acme.model;\npublic class Pet {}\n")
    write(
        tree,
        f"{MAIN}/web/OwnerController.java",
        "package com.acme.web;\n\nimport com.acme.model.*;\n\npublic class OwnerController {}\n",
    )

    assert graph_for(tree).files[f"{MAIN}/web/OwnerController.java"] == {
        f"{MAIN}/model/Owner.java",
        f"{MAIN}/model/Pet.java",
    }


def test_a_layer_violation_in_java_is_caught(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate coming on for the fifth language. This is what zero edges bought: a contract
    with nothing to judge, reporting `passed` on a tree that breaks it."""
    from oxn.check import run_check

    write(
        tree,
        f"{MAIN}/model/Owner.java",
        "package com.acme.model;\n\nimport com.acme.web.OwnerController;\n"
        "\npublic class Owner {}\n",
    )
    (tree / "oxn.yaml").write_text(
        f"layers:\n  web: ['{MAIN}/web/*']\n  model: ['{MAIN}/model/*']\n"
        "contracts:\n  - name: web-over-model\n    kind: layered\n    order: [web, model]\n"
    )
    monkeypatch.chdir(tree)

    report = run_check([f"{MAIN}/model/Owner.java"], config=Config.load(tree))

    assert {f.key for f in report.findings if f.rule.startswith("contract:")} == {
        f"contract:web-over-model|{MAIN}/model/Owner.java|"
        f"{MAIN}/model/Owner.java -> {MAIN}/web/OwnerController.java"
    }
