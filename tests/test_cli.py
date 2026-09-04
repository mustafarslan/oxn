"""End-to-end behaviour of the ``oxn`` entry point."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

import oxn


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "oxn.cli", *args], capture_output=True, text=True)


def test_version_is_pep440() -> None:
    parts = oxn.__version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_check_json_emits_valid_json(tmp_path) -> None:
    """The hook parses this output. It must be JSON on stdout, always."""
    (tmp_path / "calm.py").write_text("def one(a):\n    return a\n")
    result = subprocess.run(
        [sys.executable, "-m", "oxn", "check", "--json", "calm.py"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["oxn_version"] == oxn.__version__
    assert payload["paths"] == ["calm.py"]
    assert payload["status"] == "PASSED"
    assert payload["violations"] == []


def test_a_violation_exits_two_so_the_hook_can_feed_it_back(tmp_path) -> None:
    """The whole enforcement model rests on this number.

    A Claude Code `PostToolUse` hook treats exit 2 as "tell the agent about this"; anything
    else is either silence or a broken tool. 1 is reserved for OXN itself failing, which is
    a different situation and must not read as a code violation.
    """
    (tmp_path / "tangled.py").write_text(
        "def tangled(rows):\n"
        + "".join(f"{' ' * (4 + 4 * n)}for x{n} in rows:\n" for n in range(6))
        + " " * 28
        + "print(x5)\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "oxn", "check", "--json", "tangled.py"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 2, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "FAILED"
    assert payload["violations"], "a failing check must say what failed"
    assert all("message" in violation for violation in payload["violations"])


def test_a_missing_path_is_an_error_not_a_violation(tmp_path) -> None:
    """Exit 1, because OXN could not answer -- not exit 2, which claims the code is wrong."""
    result = subprocess.run(
        [sys.executable, "-m", "oxn", "check", "--json", "nope.py"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert "nope.py" in payload["errors"]
    assert payload["violations"] == []


def test_bare_invocation_shows_help() -> None:
    result = _run()
    assert "gatekeeper" in (result.stdout + result.stderr).lower()


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["check", "--json"], True),
        (["check", "--json", "a.py"], True),
        (["check", "a.py"], False),
        (["version"], False),
        ([], False),
    ],
)
def test_fast_path_detection(argv: list[str], expected: bool) -> None:
    from oxn.cli import _is_fast_path

    assert _is_fast_path(argv) is expected


def test_parse_json_reports_the_skeleton(tmp_path) -> None:
    source = tmp_path / "m.py"
    source.write_text("class C:\n    def m(self, a):\n        pass\n")
    result = subprocess.run(
        [sys.executable, "-m", "oxn.cli", "parse", "--json", str(source)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "OK"
    kinds = [e["kind"] for f in payload["files"] for e in f["entities"]]
    assert kinds == ["module", "class", "method"]


def test_parse_reports_missing_paths_without_crashing(tmp_path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "oxn.cli", "parse", "--json", "nope.py"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "ERROR"
    assert "nope.py" in payload["errors"]


def test_module_invocation_works() -> None:
    """``python -m oxn`` must behave exactly like the console script."""
    result = subprocess.run(
        [sys.executable, "-m", "oxn", "version"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert oxn.__version__ in result.stdout


def test_module_invocation_supports_the_json_fast_path(tmp_path) -> None:
    """A hook may invoke OXN this way, so the fast path has to work here too."""
    (tmp_path / "calm.py").write_text("def one(a):\n    return a\n")
    result = subprocess.run(
        [sys.executable, "-m", "oxn", "check", "--json"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["oxn_version"] == oxn.__version__


def test_unknown_granularity_is_rejected_not_silently_defaulted(tmp_path) -> None:
    """A typo must not produce plausible-looking output for the wrong granularity."""
    (tmp_path / "m.py").write_text("def f():\n    pass\n")
    result = subprocess.run(
        [sys.executable, "-m", "oxn", "arch", "--json", "--by", "nonsense", "."],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "ERROR"
    assert "nonsense" in payload["errors"]


# ---- the PostToolUse payload on stdin ----------------------------------------------------


def _hook(directory, payload: str | None, *args: str) -> dict:
    """`oxn check --json` as the hook runs it: no path, the payload on stdin."""
    result = subprocess.run(
        [sys.executable, "-m", "oxn.cli", "check", "--json", *args],
        capture_output=True,
        text=True,
        cwd=directory,
        input=payload if payload is not None else "",
    )
    assert result.returncode in (0, 2), result.stderr
    return json.loads(result.stdout)


def test_the_payload_names_the_file_the_agent_edited(tmp_path) -> None:
    (tmp_path / "edited.py").write_text("def one(a):\n    return a\n")
    (tmp_path / "untouched.py").write_text("def two(a):\n    return a\n")
    payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "edited.py"}})
    assert _hook(tmp_path, payload)["paths"] == ["edited.py"]


def test_a_payload_that_edited_no_file_checks_nothing_rather_than_everything(tmp_path) -> None:
    """The trap this guards: a `"."` fallback here reinstates the whole-tree walk.

    The matcher in `.claude/settings.json` can be widened to a tool that writes no file, and
    the payload shape is not ours to control. Falling back to the repository would turn every
    such invocation into seconds of work with nothing to report -- which is precisely the bug
    this branch was written to remove.
    """
    (tmp_path / "bystander.py").write_text("def one(a):\n    return a\n")
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    report = _hook(tmp_path, payload)
    assert report["paths"] == []
    assert report["status"] == "PASSED"


def test_a_file_the_edit_removed_is_not_reported_as_a_missing_path(tmp_path) -> None:
    """Deleting a file is a legitimate edit; failing the hook for it is not legitimate."""
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": "gone.py"}})
    report = _hook(tmp_path, payload)
    assert report["errors"] == {}
    assert report["paths"] == []


def test_an_explicit_path_wins_over_the_payload(tmp_path) -> None:
    """CI and humans pass paths, and a stray payload on stdin must not redirect them."""
    (tmp_path / "asked.py").write_text("def one(a):\n    return a\n")
    (tmp_path / "edited.py").write_text("def two(a):\n    return a\n")
    payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "edited.py"}})
    assert _hook(tmp_path, payload, "asked.py")["paths"] == ["asked.py"]


def test_empty_stdin_still_means_the_whole_tree(tmp_path) -> None:
    """No payload is the manual invocation, and `oxn check --json` there means everything."""
    (tmp_path / "one.py").write_text("def one(a):\n    return a\n")
    (tmp_path / "two.py").write_text("def two(a):\n    return a\n")
    assert sorted(_hook(tmp_path, "")["paths"]) == ["one.py", "two.py"]


def test_stdin_that_is_not_a_hook_payload_is_ignored(tmp_path) -> None:
    """Something else on the pipe is not a reason to check nothing."""
    (tmp_path / "one.py").write_text("def one(a):\n    return a\n")
    assert _hook(tmp_path, "not json at all\n")["paths"] == ["one.py"]


def test_an_open_pipe_that_never_closes_does_not_hang_the_hook(tmp_path) -> None:
    """The deadlock that reading stdin introduced, as a test.

    `oxn check --json` inside an ordinary shell pipeline inherits a pipe that nobody writes
    to and nobody closes. Asking it for EOF waits forever: the first version of the payload
    read hung OXN's own gate at 0% CPU until it was killed by hand. "Nothing to read" and "a
    payload not written yet" are indistinguishable on a descriptor, so the only safe answer
    is to wait briefly and then get on with it.
    """
    (tmp_path / "one.py").write_text("def one(a):\n    return a\n")
    process = subprocess.Popen(
        [sys.executable, "-m", "oxn.cli", "check", "--json"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=tmp_path,
    )
    # Deliberately *not* `communicate()`: that closes stdin, which hands the process the EOF
    # it was hanging for and makes this test pass against the very bug it names. The pipe is
    # left open, exactly as a shell leaves it. Output is small enough not to fill its buffer.
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        pytest.fail("`oxn check --json` blocked on an open stdin pipe instead of giving up")
    assert process.stdout is not None
    stdout = process.stdout.read()
    assert process.returncode in (0, 2), process.stderr.read() if process.stderr else ""
    assert json.loads(stdout)["paths"] == ["one.py"], "no payload arrived; the tree is the answer"
    process.stdin.close() if process.stdin else None
