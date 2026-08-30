"""`oxn init` must never cost a developer something they wrote.

The failure mode this guards is specific and was in `idea.md`'s original blueprint:
overwriting `CLAUDE.md` on first run. In an agent-assisted repository that file is often the
most valuable text in the tree, and a tool that destroys it once is a tool nobody installs
twice. Every test here is about *not* destroying something.
"""

from __future__ import annotations

import json

from oxn.init import HOOK_COMMAND, MARKER_BEGIN, MARKER_END, run_init


def test_a_fresh_repository_gets_everything(tmp_path) -> None:
    report = run_init(tmp_path)
    assert "oxn.yaml" in report.created
    assert "CLAUDE.md" in report.created
    assert ".claude/settings.json" in report.created
    assert (tmp_path / "oxn.yaml").exists()


def test_running_twice_changes_nothing(tmp_path) -> None:
    run_init(tmp_path)
    second = run_init(tmp_path)
    assert not second.created and not second.updated
    assert set(second.unchanged) >= {"oxn.yaml", "CLAUDE.md", ".claude/settings.json"}


def test_an_existing_config_is_never_overwritten(tmp_path) -> None:
    """Someone's tuned ceilings are not ours to replace."""
    (tmp_path / "oxn.yaml").write_text("ceilings:\n  cognitive_complexity: 25\n")
    run_init(tmp_path)
    assert "25" in (tmp_path / "oxn.yaml").read_text()


def test_hand_written_claude_instructions_survive(tmp_path) -> None:
    (tmp_path / "CLAUDE.md").write_text("# House rules\n\nAlways use tabs. Never mock the clock.\n")
    run_init(tmp_path)
    text = (tmp_path / "CLAUDE.md").read_text()
    assert "Always use tabs" in text
    assert "Never mock the clock" in text
    assert MARKER_BEGIN in text and MARKER_END in text


def test_the_section_is_replaced_in_place_not_appended(tmp_path) -> None:
    """A re-run must refresh OXN's section, not stack a second copy under the first."""
    run_init(tmp_path)
    path = tmp_path / "CLAUDE.md"
    path.write_text(path.read_text().replace("Run `oxn check` yourself", "STALE TEXT"))
    run_init(tmp_path)

    text = path.read_text()
    assert text.count(MARKER_BEGIN) == 1
    assert "STALE TEXT" not in text


def test_text_after_the_section_is_preserved(tmp_path) -> None:
    run_init(tmp_path)
    path = tmp_path / "CLAUDE.md"
    path.write_text(path.read_text() + "\n## Deployment\n\nAsk before touching prod.\n")
    run_init(tmp_path)
    assert "Ask before touching prod." in path.read_text()


def test_an_existing_hook_is_kept(tmp_path) -> None:
    """Merging, not replacing: someone else's hook is not ours to remove."""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "mine"}]}
                    ]
                }
            }
        )
    )
    run_init(tmp_path)

    written = json.loads(settings.read_text())
    commands = [
        hook["command"] for entry in written["hooks"]["PostToolUse"] for hook in entry["hooks"]
    ]
    assert "mine" in commands
    assert HOOK_COMMAND in commands


def test_the_hook_is_not_installed_twice(tmp_path) -> None:
    run_init(tmp_path)
    run_init(tmp_path)
    written = (tmp_path / ".claude" / "settings.json").read_text()
    assert written.count(HOOK_COMMAND) == 1


def test_unrelated_settings_are_left_alone(tmp_path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"model": "opus", "hooks": {}}))
    run_init(tmp_path)
    assert json.loads(settings.read_text())["model"] == "opus"


def test_unreadable_settings_are_reported_not_clobbered(tmp_path) -> None:
    """If we cannot understand the file, we do not get to rewrite it."""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{ not json")
    report = run_init(tmp_path)
    assert settings.read_text() == "{ not json"
    assert any("not valid JSON" in note for note in report.notes)


def test_no_hook_leaves_settings_untouched(tmp_path) -> None:
    report = run_init(tmp_path, with_hook=False)
    assert not (tmp_path / ".claude" / "settings.json").exists()
    assert any("--no-hook" in note for note in report.notes)


def test_the_mcp_client_is_not_wired_to_a_server_that_does_not_exist(tmp_path) -> None:
    """P2.5's original plan writes `.mcp.json`; the MCP server lands in P9.

    Wiring a client to a missing server produces a broken tool in someone's editor and a bug
    report about OXN. `init` only wires surfaces that work today.
    """
    run_init(tmp_path)
    assert not (tmp_path / ".mcp.json").exists()


def test_the_baseline_is_never_gitignored(tmp_path) -> None:
    """The ratchet is shared state. Uncommitted, it forgives a different set per developer."""
    run_init(tmp_path)
    ignored = (tmp_path / ".gitignore").read_text()
    assert ".oxn/cache/" in ignored
    assert "!.oxn/baseline.json" in ignored


def test_an_existing_gitignore_keeps_its_entries(tmp_path) -> None:
    (tmp_path / ".gitignore").write_text("*.pyc\n__pycache__/\n")
    run_init(tmp_path)
    ignored = (tmp_path / ".gitignore").read_text()
    assert "*.pyc" in ignored
    assert ".oxn/cache/" in ignored
