"""The architectural tier, driven from the installed script.

`test_gate.py` covers the hook path: ceilings, measured per entity, on one file. That is
the tier the `PostToolUse` hook can answer, and it is deliberately the cheap one -- a
file's own text settles it.

`--deep` is the other half, and until this module nothing in the e2e lane ran it. It
answers the two questions one file cannot: whether an import crosses a layer boundary the
contract forbids, and whether the import graph has a cycle. Both need the whole graph, both
are what `oxn.yaml`'s `layers` and `contracts` sections exist to declare, and both are the
headline feature in the README -- so "we install it and it gates" was being asserted
without the architectural half ever leaving the working tree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from harness import Installed, git_init

pytestmark = pytest.mark.e2e

#: Two layers, outermost first, and the contract that orders them. Deliberately the same
#: shape as OXN's own `oxn.yaml` -- one `layered` contract over an ordered list -- because
#: a fixture that exercises a different shape tests a feature nobody ships.
LAYERED = """\
ceilings:
  cognitive_complexity: 12
layers:
  surfaces: ["src/app/*"]
  core: ["src/core/*"]
contracts:
  - name: surfaces-over-core
    kind: layered
    order: [surfaces, core]
"""


#: The illegal direction: the inner layer importing the outer one. Hoisted because both
#: tiers are asked about the same edge, and an edge spelled two ways is two fixtures.
REACHES_BACK_OUT = (
    "from src.app.handler import handle\n\n\n"
    "class Item:\n    def go(self):\n        return handle()\n"
)


def _json(result) -> dict:
    """`--json` prints a human report first; the JSON is the last object on stdout."""
    return json.loads(result.stdout[result.stdout.index("{") :])


@pytest.fixture
def layered(tmp_path: Path) -> Path:
    """A tree whose imports obey the contract it declares."""
    (tmp_path / "oxn.yaml").write_text(LAYERED)
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "core").mkdir(parents=True)
    (tmp_path / "src" / "__init__.py").write_text("")
    (tmp_path / "src" / "app" / "__init__.py").write_text("")
    (tmp_path / "src" / "core" / "__init__.py").write_text("")
    # The legal direction: a surface reaching inward.
    (tmp_path / "src" / "app" / "handler.py").write_text(
        "from src.core.model import Item\n\n\ndef handle() -> Item:\n    return Item()\n"
    )
    (tmp_path / "src" / "core" / "model.py").write_text("class Item:\n    pass\n")
    git_init(tmp_path)
    return tmp_path


def test_a_tree_that_obeys_its_contract_passes_the_deep_tier(
    installed: Installed, layered: Path
) -> None:
    """The control. A contract that fails a conforming tree is worse than no contract."""
    result = installed.run("check", "--deep", "--json", "src", cwd=layered)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _json(result)["violations"] == [], "a conforming tree reported a violation"


def test_an_import_across_a_layer_boundary_is_rejected_and_the_edge_named(
    installed: Installed, layered: Path
) -> None:
    """The inward layer reaches back out. The report must name the edge, not just the file.

    Naming the edge is the whole contract of this rule: "core/model.py is bad" is not
    something an agent can act on, and `core/model.py -> app/handler.py` is.
    """
    (layered / "src" / "core" / "model.py").write_text(REACHES_BACK_OUT)
    result = installed.run("check", "--deep", "--json", "src", cwd=layered)

    assert result.returncode == 2, f"the layer violation did not block: {result.stdout}"
    violations = _json(result)["violations"]
    assert violations, "blocked but reported no violation"
    rendered = json.dumps(violations)
    assert "app" in rendered and "core" in rendered, f"the edge is not named: {rendered}"


def test_the_hook_tier_already_answers_a_file_s_own_outgoing_import(
    installed: Installed, layered: Path
) -> None:
    """Where the line between the two tiers actually falls, pinned rather than assumed.

    `oxn.yaml` draws it: the hook answers ceilings *and the layer contracts one file's own
    imports can settle*, because a file's own text names everything it imports. Only the
    whole graph can add edges *into* a file, or close a cycle. So a forbidden outgoing edge
    must block without `--deep` -- that is the claim CLAUDE.md makes to every agent working
    in an OXN repository, and this is the e2e lane's copy of it.
    """
    (layered / "src" / "core" / "model.py").write_text(REACHES_BACK_OUT)
    shallow = installed.run("check", "--json", "src", cwd=layered)
    assert shallow.returncode == 2, (
        "the outgoing edge is settleable from the file's own text and did not block on the "
        f"hook path: {shallow.stdout[-400:]}"
    )


#: `acyclic` is the one contract kind that works over *directory components* rather than
#: files, because two files in one package importing each other is both common and usually
#: harmless. So the fixture needs two packages, not two modules.
ACYCLIC = """\
ceilings:
  cognitive_complexity: 12
layers:
  left: ["src/left/*"]
  right: ["src/right/*"]
contracts:
  - name: no-rings
    kind: acyclic
"""


def _package(root: Path, name: str, body: str) -> None:
    (root / "src" / name).mkdir(parents=True)
    (root / "src" / name / "__init__.py").write_text("")
    (root / "src" / name / "mod.py").write_text(body)


def test_an_import_ring_between_packages_is_reported_as_one(
    installed: Installed, tmp_path: Path
) -> None:
    """The finding no single file can produce: each import is fine, the ring is not.

    Neither file is doing anything a file-local check could object to -- the violation
    exists only in the graph, which is precisely why it belongs to `--deep` and why the
    e2e lane had a hole here.
    """
    (tmp_path / "oxn.yaml").write_text(ACYCLIC)
    (tmp_path / "src" / "__init__.py").parent.mkdir(parents=True, exist_ok=True)
    _package(
        tmp_path, "left", "from src.right.mod import there\n\n\ndef here():\n    return there()\n"
    )
    _package(
        tmp_path, "right", "from src.left.mod import here\n\n\ndef there():\n    return here\n"
    )
    (tmp_path / "src" / "__init__.py").write_text("")
    git_init(tmp_path)

    result = installed.run("check", "--deep", "--json", "src", cwd=tmp_path)
    report = _json(result)

    assert "architectural check skipped" not in json.dumps(report.get("diagnostics", [])), (
        "the contract never ran, so a pass here would mean nothing"
    )
    assert result.returncode == 2, f"the ring did not block: {result.stdout[-400:]}"
    rendered = json.dumps(report["violations"])
    assert "no-rings" in rendered, f"the contract that fired is not named: {rendered[:400]}"
    assert "cycle" in rendered, f"the finding does not say what it found: {rendered[:400]}"
