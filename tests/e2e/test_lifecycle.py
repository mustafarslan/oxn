"""`oxn init` into a repository that already has a life, and what removal leaves behind.

`tests/test_init.py` covers the pieces. This covers the sequence a person actually runs,
against files they wrote themselves, through the installed script -- and then takes it all
out again, which is the leg no command implements and therefore no test has ever checked.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from harness import FIXTURES, Installed, git_init, plant

pytestmark = pytest.mark.e2e

THEIR_CLAUDE_MD = "# House rules\n\nWe use tabs. Do not argue.\n"
THEIR_SETTINGS = {
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}]
    }
}

#: What `oxn init` creates outright, plus `.oxn/` which the first `check` writes. These are
#: removable by deletion. `.gitignore` is in this list because `init` creates it when absent
#: -- when one already exists it is *appended to* instead, and removal then means editing it
#: rather than deleting it, which is why the list below is not the whole removal story.
CREATED = (".oxn", "oxn.yaml", ".mcp.json", ".gitignore")

#: Files `init` merges into rather than creates: removal means putting back what was there.
MERGED = ("CLAUDE.md", ".claude/settings.json")


@pytest.fixture
def lived_in(tmp_path: Path) -> Path:
    """A repository with a CLAUDE.md and a hook of its own, committed."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    return 1\n")
    (tmp_path / "CLAUDE.md").write_text(THEIR_CLAUDE_MD)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(THEIR_SETTINGS, indent=2))
    git_init(tmp_path)
    return tmp_path


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


def test_init_keeps_what_the_user_already_wrote(installed: Installed, lived_in: Path) -> None:
    """Their prose survives, their hook survives, and OXN's section is added alongside."""
    assert installed.run("init", cwd=lived_in).returncode == 0

    claude_md = (lived_in / "CLAUDE.md").read_text()
    assert claude_md.startswith(THEIR_CLAUDE_MD), "hand-written CLAUDE.md was not preserved"
    assert "<!-- oxn:begin -->" in claude_md

    settings = json.loads((lived_in / ".claude" / "settings.json").read_text())
    assert settings["hooks"]["PreToolUse"] == THEIR_SETTINGS["hooks"]["PreToolUse"]
    assert "PostToolUse" in settings["hooks"], "the gate hook was not installed"


def test_init_wires_the_mcp_server(installed: Installed, lived_in: Path) -> None:
    installed.run("init", cwd=lived_in)
    servers = json.loads((lived_in / ".mcp.json").read_text())["mcpServers"]
    assert "oxn" in servers
    assert "serve" in servers["oxn"]["args"]


def test_running_init_twice_changes_nothing(installed: Installed, lived_in: Path) -> None:
    """Idempotent in the only sense that matters: the second run produces no diff."""
    installed.run("init", cwd=lived_in)
    _git(lived_in, "add", "-A")
    subprocess.run(["git", "commit", "--quiet", "-m", "init"], cwd=lived_in, check=True)

    assert installed.run("init", cwd=lived_in).returncode == 0

    assert _git(lived_in, "status", "--porcelain") == "", "a second init rewrote something"


def test_the_hook_command_init_wrote_is_one_that_runs(installed: Installed, lived_in: Path) -> None:
    """A hook wired to a command that does not resolve is a gate that never fires."""
    installed.run("init", cwd=lived_in)
    settings = json.loads((lived_in / ".claude" / "settings.json").read_text())
    hooks = settings["hooks"]["PostToolUse"]
    command = next(
        hook["command"]
        for entry in hooks
        for hook in entry["hooks"]
        if "oxn" in hook.get("command", "")
    )
    assert "check" in command and "--json" in command, command


def test_a_shred_is_caught_rather_than_credited(installed: Installed, project: Path) -> None:
    """The rule an agent meets while trying to satisfy the others.

    `route` is under the ceiling on its own; totalled with the private, trivial helpers only
    it calls it is 13. A gate that missed this would reward relocating complexity.
    """
    plant(FIXTURES / "shredded.py.txt", project / "src")

    result = installed.run("check", "--json", cwd=project)

    assert result.returncode == 2, "a shred passed the gate"
    violations = json.loads(result.stdout[result.stdout.index("{") :])["violations"]
    assert [v["rule"] for v in violations] == ["shredding"]
    assert violations[0]["value"] == 13.0
    assert violations[0]["entity"].endswith(".route")


def test_the_same_tree_measures_the_same_twice(installed: Installed, project: Path) -> None:
    """Determinism, and then the same answer with the cache thrown away.

    A cache that returns a stale number is the one defect nothing else here would catch.
    """
    plant(FIXTURES / "over_ceiling.py.txt", project / "src")
    first = installed.run("check", "--json", cwd=project).stdout
    second = installed.run("check", "--json", cwd=project).stdout
    assert first == second, "two runs over one tree disagreed"

    cache = project / ".oxn" / "cache"
    assert (cache / "graph.db").exists(), "no cache was written, so clearing it proves nothing"
    subprocess.run(["rm", "-rf", str(cache)], check=True)
    assert installed.run("check", "--json", cwd=project).stdout == first, "the cache went stale"


def test_removing_what_init_added_restores_the_repository(
    installed: Installed, lived_in: Path
) -> None:
    """There is no `oxn uninstall`, so this pins what a person has to delete by hand.

    If this ever fails, the honest answer is a documented removal list -- or a command --
    rather than a looser assertion.
    """
    installed.run("init", cwd=lived_in)
    installed.run("check", "--json", cwd=lived_in)

    subprocess.run(["rm", "-rf", *(str(lived_in / name) for name in CREATED)], check=True)
    _git(lived_in, "checkout", "--", *MERGED)

    assert _git(lived_in, "status", "--porcelain") == "", "removal left the repository dirty"
    assert (lived_in / "CLAUDE.md").read_text() == THEIR_CLAUDE_MD
