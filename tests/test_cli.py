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


def test_check_json_emits_valid_json() -> None:
    """The hook parses this output. It must be JSON on stdout, always."""
    result = _run("check", "--json", "some/file.py")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["oxn_version"] == oxn.__version__
    assert payload["paths"] == ["some/file.py"]
    assert "violations" in payload


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


def test_module_invocation_supports_the_json_fast_path() -> None:
    """A hook may invoke OXN this way, so the fast path has to work here too."""
    result = subprocess.run(
        [sys.executable, "-m", "oxn", "check", "--json"], capture_output=True, text=True
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
