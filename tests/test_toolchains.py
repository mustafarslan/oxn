"""The gauntlet running a bed's own commands, and the two edges that decide a verdict.

`_run_toolchain` ran this project's pytest, ruff and mypy against whatever it was handed.
With one bed that was invisible; with six it would have graded a Rust repair with Python's
linter. It runs what the bed declares now, and the interesting behaviour is what happens when
a check is *absent* versus when its *tool* is:

* a check the bed does not declare is **skipped**, and cannot fail a repair -- a Go module
  has no type checker, and holding it to `types_pass` would fail every repair forever;
* a declared check whose tool is missing is a **failure**, because then the repair was not
  verified, and a green row for a command that never ran is the same defect as measuring a
  stale cache.

Nothing here runs a real test suite. The dispatch and the verdict are what can be silently
wrong; whether `cargo test` passes on ripgrep today is ripgrep's business.
"""

from __future__ import annotations

import shutil

import pytest
from tests.test_dogfood import load_harness


@pytest.fixture(scope="module")
def harness():
    return load_harness()


@pytest.fixture
def sandbox(harness, tmp_path, monkeypatch):
    """A sandbox over an empty directory, with no virtualenv to build."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "marker.txt").write_text("x")
    monkeypatch.setattr(harness, "SCRATCH", tmp_path / "scratch")
    box = harness.Sandbox("probe", root=source, venv=False)
    box.path = tmp_path / "box"
    shutil.copytree(source, box.path)
    return box


def test_a_command_without_dash_m_runs_as_itself(harness, sandbox) -> None:
    """`go test` has no interpreter to route through, and assuming one is why there was one bed."""
    finished = sandbox.run("echo", "hello")
    assert finished.returncode == 0
    assert finished.stdout.strip() == "hello"


def test_a_dash_m_command_still_routes_through_the_bed_interpreter(harness, sandbox) -> None:
    """The Python beds' checks are written against their own interpreter, and stay that way."""
    argv_seen = {}

    def fake(argv, **kwargs):
        argv_seen["argv"] = argv
        return __import__("subprocess").CompletedProcess(argv, 0, "", "")

    import subprocess as sp

    original = sp.run
    sp.run = fake
    try:
        sandbox.run("-m", "pytest", "-q")
    finally:
        sp.run = original
    assert argv_seen["argv"][0] == str(sandbox.python)
    assert argv_seen["argv"][1:] == ["-m", "pytest", "-q"]


def test_a_check_the_bed_does_not_declare_cannot_fail_the_repair(harness, sandbox) -> None:
    """Go declares no type checker. `types_pass` stays False and must not count."""
    result = harness.GauntletResult()
    harness._run_toolchain(sandbox, result, (("tests", "echo", "ok"),))
    assert result.tests_pass
    assert result.skipped == {"lint", "types"}
    assert result.toolchain_pass, "an undeclared check must not fail a bed that has no such tool"


def test_a_declared_check_whose_tool_is_missing_is_a_failure(harness, sandbox) -> None:
    """The opposite edge. Unverified is not the same as nothing to verify.

    A missing `cargo` means the repair was never checked, and reporting that as a pass is how
    an arm table fills with rows nobody measured.
    """
    result = harness.GauntletResult()
    harness._run_toolchain(sandbox, result, (("tests", "definitely-not-a-real-tool-xyz"),))
    assert not result.tests_pass
    assert not result.toolchain_pass
    assert result.failures and "not a real tool" in result.failures[0].lower().replace("-", " ")


def test_two_commands_under_one_label_must_both_pass(harness, sandbox) -> None:
    """`lint` is ruff check *and* ruff format for this project, and the second must bite."""
    result = harness.GauntletResult()
    harness._run_toolchain(sandbox, result, (("lint", "echo", "fine"), ("lint", "false")))
    assert not result.lint_pass, "a label passes only if every command under it does"


@pytest.mark.parametrize("bed_name", ("go-kit", "rust-ripgrep", "typescript-nest"))
def test_each_beds_toolchain_is_actually_installed_here(harness, bed_name) -> None:
    """Not a claim about the beds; a check that this machine can run them at all.

    If it cannot, the arm table for that bed would be all failures with `not on PATH` in
    every row -- which is the honest reading, and worth knowing before a long run rather
    than after one.
    """
    tools = {command[0] for _label, *command in harness.BEDS[bed_name].verify}
    missing = {tool for tool in tools if shutil.which(tool) is None}
    assert not missing, f"{bed_name} needs {sorted(missing)} on PATH"


def test_two_runs_of_one_target_do_not_share_a_sandbox(harness) -> None:
    """They did, and each deletes its sandbox on exit, so one destroyed the other's tree.

    The path was `SCRATCH / target.leaf`. A second run of the same target -- which is the
    *normal* case for an experiment, where six arms repair the same function -- landed in
    the same directory, and `_repair_one` cleans up however it exits. It surfaced as a
    `FileNotFoundError` writing a candidate into a directory that had just been removed, in
    a pilot arm five minutes into its attempt.

    Keyed by process now. Two sandboxes for one target must not be the same place.
    """
    import os

    mine = harness.Sandbox("walk")
    assert str(os.getpid()) in mine.path.name, "the name must distinguish this process"
    assert mine.path.name.startswith("walk"), "and still say which target it is"


def test_a_sandbox_still_lands_under_the_scratch_root(harness) -> None:
    """Uniqueness must not push it somewhere a cleanup would miss."""
    assert harness.Sandbox("walk").path.parent == harness.SCRATCH
