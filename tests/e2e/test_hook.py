"""The contract between Claude Code's PostToolUse hook and the installed `oxn`.

The hook does not pass a path. It writes a JSON payload to stdin naming the file the agent
just edited, and reads the answer back off two different streams. Every other test in this
repository passes paths as arguments, which is a route the deployed hook never takes --
`_edited_targets` records the 7.27s regression that went unnoticed for exactly that reason.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from harness import FIXTURES, SAME_SHAPE, Installed, plant

pytestmark = pytest.mark.e2e


def _payload(path: Path, *, session: str = "") -> str:
    event = {"tool_name": "Edit", "tool_input": {"file_path": str(path)}}
    if session:
        event["session_id"] = session
    return json.dumps(event)


def test_the_payload_narrows_the_check_to_the_edited_file(
    installed: Installed, project: Path
) -> None:
    """Six files in the tree, one of them edited: one file is measured, not six."""
    for source in SAME_SHAPE.values():
        plant(source, project / "src")
    edited = project / "src" / "sample.py"

    result = installed.run("check", "--json", stdin=_payload(edited), cwd=project)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["paths"] == ["src/sample.py"], "the whole tree was walked"


def test_a_violation_reaches_the_agent_on_stderr(installed: Installed, project: Path) -> None:
    """Claude Code shows *stderr* to the agent on exit 2 and files stdout in the transcript.

    A gate that blocks and explains itself only on stdout reaches the agent as
    `No stderr output`, which is the whole of P9 having happened once already.
    """
    edited = plant(FIXTURES / "over_ceiling.py.txt", project / "src")

    result = installed.run("check", "--json", stdin=_payload(edited), cwd=project)

    assert result.returncode == 2
    assert "cognitive_complexity 13" in result.stderr
    assert "above the ceiling of 12" in result.stderr
    assert json.loads(result.stdout)["violations"][0]["value"] == 13.0


def test_an_edit_to_a_file_that_is_gone_is_not_a_violation(
    installed: Installed, project: Path
) -> None:
    """A rename or a delete must not fail the hook on an otherwise legitimate edit."""
    result = installed.run(
        "check", "--json", stdin=_payload(project / "src" / "deleted.py"), cwd=project
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["violations"] == []


def test_a_payload_naming_no_file_measures_nothing_rather_than_the_tree(
    installed: Installed, project: Path
) -> None:
    """The fallback is the trap: a widened matcher must not silently walk the repository."""
    plant(FIXTURES / "over_ceiling.py.txt", project / "src")

    result = installed.run(
        "check", "--json", stdin=json.dumps({"tool_name": "Bash", "tool_input": {}}), cwd=project
    )

    assert result.returncode == 0, "an unanalysable payload walked the tree"
    assert json.loads(result.stdout)["paths"] == []


def test_malformed_stdin_does_not_crash_the_hook(installed: Installed, project: Path) -> None:
    """A broken payload must not take the agent's edit down with it.

    Unreadable stdin is the "not invoked by a hook" case, so it falls back to the tree --
    the same route a person running `oxn check --json` by hand takes.
    """
    result = installed.run("check", "--json", stdin="{not json", cwd=project)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "PASSED"
