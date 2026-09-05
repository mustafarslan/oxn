"""Layer contracts on the hook path, and the limit of what one file can prove.

Until 2026-09-05 `oxn check` answered contracts only under `--deep`, so an agent could
import across a layer boundary and the `PostToolUse` hook said nothing — every edit, for
the whole of P7 onwards. The reason was a cost that turned out not to be there: building
the import graph was assumed to mean parsing the whole tree. It does not. Resolution needs
the tree's *layout* (a directory walk) and the edited file's *text* (one parse), so the hook
can populate that file's outgoing edges and evaluate every contract rule against them.

What the hook still cannot answer is asserted here too, because a check that quietly
answers a narrower question than the one asked is worse than one that declines:

* edges **into** the measured files, from files nobody edited — `--deep` finds those;
* `cycle`, which is not a property of any single file.

The identity test is the load-bearing one. A contract finding from the hook must carry the
same key as the same finding from `--deep`, or `.oxn/baseline.json` stops being a ratchet:
an accepted layer violation would re-fire as new on the next edit of the file.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from oxn.check import run_check
from oxn.config import Config

CONFIG = """
layers:
  surfaces: ["app/*"]
  core: ["core/*"]
contracts:
  - name: layered
    kind: layered
    order: [surfaces, core]
"""

LEGAL = "from core.engine import run\n\n\ndef main():\n    return run()\n"
ILLEGAL = "from app.entry import main\n\n\ndef run():\n    return main\n"
CLEAN = "def run():\n    return 1\n"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Two layers, and a `core` module free to break the contract on demand."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "oxn.yaml").write_text(CONFIG)
    for package in ("app", "core"):
        (tmp_path / package).mkdir()
        (tmp_path / package / "__init__.py").write_text("")
    (tmp_path / "app" / "entry.py").write_text(LEGAL)
    (tmp_path / "core" / "engine.py").write_text(CLEAN)
    return tmp_path


def violation_keys(report) -> set[str]:
    return {finding.key for finding in report.findings if finding.rule.startswith("contract:")}


def test_the_hook_catches_a_layer_violation_in_the_file_it_measured(project: Path) -> None:
    """The gap this closes: `core` reaching up into `app` used to pass every hook."""
    (project / "core" / "engine.py").write_text(ILLEGAL)

    report = run_check(["core/engine.py"], config=Config.load(project))

    assert violation_keys(report) == {
        "contract:layered|core/engine.py|core/engine.py -> app/entry.py"
    }


def test_a_legal_import_is_not_a_finding(project: Path) -> None:
    """`app -> core` is the direction the contract permits, and the ordering is easy to
    read backwards -- so it is asserted rather than assumed."""
    report = run_check(["app/entry.py"], config=Config.load(project))

    assert violation_keys(report) == set()


def test_the_hook_and_deep_agree_on_a_finding_s_identity(project: Path) -> None:
    """If these keys differ, a baselined layer violation re-fires as new from the hook and
    the ratchet is quietly an amnesty."""
    (project / "core" / "engine.py").write_text(ILLEGAL)
    settings = Config.load(project)

    hook = run_check(["core/engine.py"], config=settings)
    deep = run_check(["."], config=settings, deep=True)

    assert violation_keys(hook) == violation_keys(deep)


def test_the_hook_cannot_see_an_edge_into_the_file_it_measured(project: Path) -> None:
    """The honest limit, asserted so it cannot be forgotten. Editing `app/entry.py` says
    nothing about `core/engine.py`'s illegal import of it -- that edge belongs to the file
    that made it, whose own edit was gated, and to `--deep`."""
    (project / "core" / "engine.py").write_text(ILLEGAL)
    settings = Config.load(project)

    assert violation_keys(run_check(["app/entry.py"], config=settings)) == set()
    assert violation_keys(run_check(["."], config=settings, deep=True)) != set()


def test_the_narrower_scope_is_stated_rather_than_left_to_be_inferred(project: Path) -> None:
    """Silence from a check that answered a smaller question reads exactly like conformance."""
    report = run_check(["app/entry.py"], config=Config.load(project))

    assert any("file-scoped contract check" in note for note in report.diagnostics)


def test_an_unresolvable_import_produces_no_finding(project: Path) -> None:
    """ADR-0002: only a sound measurement may block, and an import OXN could not place is
    the archetype of unsound. Guessing which layer `requests` lives in would manufacture
    violations out of every third-party dependency."""
    (project / "core" / "engine.py").write_text("import requests\n\n\ndef run():\n    return 1\n")

    report = run_check(["core/engine.py"], config=Config.load(project))

    assert violation_keys(report) == set()


def test_a_project_with_no_contracts_pays_nothing(project: Path) -> None:
    """The walk is only worth its milliseconds when a contract exists to spend them on."""
    (project / "oxn.yaml").write_text("ceilings:\n  cognitive_complexity: 12\n")
    (project / "core" / "engine.py").write_text(ILLEGAL)

    report = run_check(["core/engine.py"], config=Config.load(project))

    assert report.findings == []
    assert report.diagnostics == []


def test_the_agent_is_told_which_edge_broke_which_contract(project: Path) -> None:
    """End to end through the hook's own channel: the stderr an agent actually receives."""
    (project / "core" / "engine.py").write_text(ILLEGAL)
    payload = json.dumps(
        {"session_id": "s1", "tool_name": "Edit", "tool_input": {"file_path": "core/engine.py"}}
    )

    result = subprocess.run(
        [sys.executable, "-m", "oxn.cli", "check", "--json"],
        capture_output=True,
        text=True,
        cwd=project,
        input=payload,
    )

    assert result.returncode == 2
    assert "core/engine.py -> app/entry.py: depends on a layer above it" in result.stderr
    assert "[attempt 1 of 3]" in result.stderr


def test_a_deep_import_entry_point_is_still_an_entry_point_from_the_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`deep_import` allows named entry points, and the allow-list is glob-matched against
    known paths. Match it against the parsed files alone and the hook -- which parses one --
    finds no entry point and reports every legal import as a bypass. An empty allow-list is
    a false positive, not a strict check."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "oxn.yaml").write_text("""
layers:
  app: ["app/*"]
  pkg: ["pkg/*"]
contracts:
  - name: pkg-api
    kind: deep_import
    package: pkg
    allowed_entrypoints: ["pkg/api.py"]
""")
    for package in ("app", "pkg"):
        (tmp_path / package).mkdir()
        (tmp_path / package / "__init__.py").write_text("")
    (tmp_path / "pkg" / "api.py").write_text("value = 1\n")
    (tmp_path / "pkg" / "internal.py").write_text("secret = 2\n")
    (tmp_path / "app" / "good.py").write_text("from pkg.api import value\n")
    (tmp_path / "app" / "bad.py").write_text("from pkg.internal import secret\n")
    settings = Config.load(tmp_path)

    assert violation_keys(run_check(["app/good.py"], config=settings)) == set()
    assert violation_keys(run_check(["app/bad.py"], config=settings)) == {
        "contract:pkg-api|app/bad.py|app/bad.py -> pkg/internal.py"
    }
