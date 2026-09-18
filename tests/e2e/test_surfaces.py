"""Every read-only surface, driven from the installed script, and the MCP entry `init` wrote.

`tests/test_server.py` already drives the protocol. What it cannot check is whether the
command `oxn init` puts in `.mcp.json` is one that starts a server on a machine where OXN
was installed rather than checked out -- which is the only way anyone but a contributor
ever runs it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from harness import SAME_SHAPE, Installed, plant

pytestmark = pytest.mark.e2e

#: The commands that answer questions rather than judging, and the key each must return.
REPORTS = (
    ("metrics", "entities"),
    ("volume", "status"),
    ("arch", "status"),
    ("calls", "status"),
    ("classes", "status"),
    ("health", "status"),
    ("index", "status"),
)


@pytest.fixture
def populated(installed: Installed, project: Path) -> Path:
    for source in SAME_SHAPE.values():
        plant(source, project / "src")
    return project


@pytest.mark.parametrize(("command", "key"), REPORTS)
def test_every_report_answers_in_valid_json(
    installed: Installed, populated: Path, command: str, key: str
) -> None:
    """`--json` is the contract CI and the MCP server both consume; prose on stdout breaks it."""
    result = installed.run(command, "src", "--json", cwd=populated)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert key in payload, f"{command} --json returned {sorted(payload)}"


def test_calibration_declares_the_evidence_behind_every_ceiling(
    installed: Installed, populated: Path
) -> None:
    """The honesty surface: a parameter with no observations behind it must say so."""
    result = installed.run("calibration", "--json", cwd=populated)
    assert result.returncode == 0, result.stderr
    parameters = json.loads(result.stdout)["parameters"]
    assert parameters, "calibration reported no parameters"
    for parameter in parameters:
        assert parameter["evidence"] in {"judgement", "literature", "measured", "calibrated"}
        assert "observations" in parameter, parameter["name"]


def test_doctor_reports_on_a_working_install(installed: Installed, populated: Path) -> None:
    """What a user runs when something is wrong; it must not be the thing that is wrong."""
    result = installed.run("doctor", cwd=populated)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


def test_parse_reads_every_launch_language(installed: Installed, populated: Path) -> None:
    """Six grammars load from the wheel -- tree-sitter parsers are fetched, not vendored."""
    result = installed.run("parse", "src", "--json", cwd=populated)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload.get("errors", {}) == {}, payload.get("errors")


def _exchange(command: list[str], root: Path, bin_dir: Path, *messages: dict) -> list[dict]:
    """Drive the server over stdio the way a client does, and read the replies back."""
    environment = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    result = subprocess.run(
        command,
        cwd=root,
        input="".join(json.dumps(message) + "\n" for message in messages),
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
        check=False,
    )
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def test_the_mcp_entry_init_wrote_starts_a_server(installed: Installed, populated: Path) -> None:
    """Launch it by the command in `.mcp.json`, not by one this test made up."""
    installed.run("init", cwd=populated)
    entry = json.loads((populated / ".mcp.json").read_text())["mcpServers"]["oxn"]

    replies = _exchange(
        [entry["command"], *entry["args"]],
        populated,
        installed.oxn.parent,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        },
    )

    assert replies, "the wired command produced no reply"
    assert replies[0]["result"]["serverInfo"]["name"] == "oxn"


def test_the_server_answers_about_the_repository_it_was_started_in(
    installed: Installed, populated: Path
) -> None:
    """`get_architectural_context` must describe this tree, not the one OXN lives in."""
    installed.run("init", cwd=populated)
    entry = json.loads((populated / ".mcp.json").read_text())["mcpServers"]["oxn"]

    replies = _exchange(
        [entry["command"], *entry["args"]],
        populated,
        installed.oxn.parent,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "get_architectural_context",
                "arguments": {"task": "edit classify", "files": ["src/sample.py"]},
            },
        },
    )

    answer = next(reply for reply in replies if reply.get("id") == 2)
    assert "error" not in answer, answer
    assert "cognitive_complexity" in json.dumps(answer["result"])
