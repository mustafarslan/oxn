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


def test_the_mcp_server_is_wired_now_that_it_exists(tmp_path) -> None:
    """P2.5 withheld `.mcp.json` because the server did not exist and a client wired to a
    missing server is a broken tool in someone's editor. It exists as of P9."""
    report = run_init(tmp_path)
    assert ".mcp.json" in report.created
    written = json.loads((tmp_path / ".mcp.json").read_text())
    assert written["mcpServers"]["oxn"] == {"command": "oxn", "args": ["serve"]}


def test_another_teams_mcp_servers_are_not_replaced(tmp_path) -> None:
    """`.mcp.json` is usually committed and usually already has entries in it."""
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"postgres": {"command": "pg-mcp"}}})
    )
    report = run_init(tmp_path)
    written = json.loads((tmp_path / ".mcp.json").read_text())
    assert written["mcpServers"]["postgres"] == {"command": "pg-mcp"}
    assert "oxn" in written["mcpServers"]
    assert ".mcp.json" in report.updated


def test_a_hand_edited_oxn_entry_is_left_alone(tmp_path) -> None:
    """A pinned interpreter or a wrapper script is someone solving the PATH problem their
    own way, and overwriting it every `init` would undo the fix on every run."""
    pinned = {"command": "/opt/venv/bin/oxn", "args": ["serve"]}
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"oxn": pinned}}))
    report = run_init(tmp_path)
    assert json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["oxn"] == pinned
    assert any("left alone" in note for note in report.notes)


def test_a_malformed_mcp_file_is_reported_rather_than_replaced(tmp_path) -> None:
    (tmp_path / ".mcp.json").write_text("{ not json")
    report = run_init(tmp_path)
    assert (tmp_path / ".mcp.json").read_text() == "{ not json"
    assert any(".mcp.json is not valid JSON" in note for note in report.notes)


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


def test_init_wires_the_repository_not_the_directory_it_was_run_from(tmp_path, monkeypatch) -> None:
    """A setup command run in the wrong terminal tab should not create a second project.

    `Config.load` searches upward for `oxn.yaml`, so a config left in `src/` would govern
    everything below it and nothing above -- a shadowing project inside the real one.
    `.claude/settings.json` and `.gitignore` belong at the checkout root for the same reason.
    """
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)

    monkeypatch.chdir(nested)
    run_init()

    assert (tmp_path / "oxn.yaml").is_file()
    assert (tmp_path / ".claude" / "settings.json").is_file()
    assert not (nested / "oxn.yaml").exists()


def test_init_outside_a_checkout_still_wires_where_it_stands(tmp_path, monkeypatch) -> None:
    """Not every tree is a git repository, and `oxn init` must still work in one."""
    monkeypatch.chdir(tmp_path)
    run_init()
    assert (tmp_path / "oxn.yaml").is_file()
