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
def scratch(tmp_path, monkeypatch):
    """Point `Sandbox` at this test's own scratch root, and mean it.

    `load_harness` returns a *merged namespace*, so `monkeypatch.setattr(harness, "SCRATCH",
    ...)` rebinds a copy and `Sandbox.__init__` goes on reading `gauntlet.SCRATCH`. That is
    not a hypothetical: it left fourteen real directories under `/tmp/oxn-dogfood` while a
    repair run was using that same root. Patch the module, which is what the code reads.
    """
    import sys

    root = tmp_path / "scratch"
    monkeypatch.setattr(sys.modules["gauntlet"], "SCRATCH", root)
    return root


@pytest.fixture
def sandbox(harness, tmp_path, scratch):
    """A sandbox over an empty directory, with no virtualenv to build."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "marker.txt").write_text("x")
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


# ---- the bed has to work before a repair can be judged against it -------------------------


def test_a_bed_that_fails_its_own_untouched_tree_raises_rather_than_scoring(
    harness, sandbox
) -> None:
    """The fifth defect of this family, and the one with the largest blast radius.

    `Sandbox.create` installed `-e "{path}[dev]"` -- this project's convention, applied to
    every bed. httpx has no `dev` extra, and `uv` does not refuse a missing one: it exits 0
    having installed nothing at all. So the sandbox had no `pytest`, every attempt scored
    `tests FAIL`, and a full grid would have produced a complete table of zeros with nothing
    in it to say the virtualenv was empty.

    A pristine run is the check, rather than a readiness probe, because it proves the thing
    that matters -- these exact commands, on this exact tree -- and needs no second mechanism
    kept in step with the first.
    """
    with pytest.raises(harness.SandboxNotReady, match="unmodified"):
        harness.verify_bed(sandbox, (("tests", "false"),))

    harness.verify_bed(sandbox, (("tests", "true"),))


def test_a_prepare_step_that_fails_stops_the_sandbox(harness, tmp_path, scratch) -> None:
    """`Bed.prepare` was declared, documented, and read by nothing at all."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "marker.txt").write_text("x")

    box = harness.Sandbox("probe", root=source, venv=False, prepare=(("false",),))
    with pytest.raises(harness.SandboxNotReady, match="preparing"):
        box.create()

    ran = harness.Sandbox("probe2", root=source, venv=False, prepare=(("true",),))
    ran.create()
    assert (ran.path / "marker.txt").exists()


def test_the_toolchain_will_not_borrow_another_beds_checks(harness, sandbox) -> None:
    """It read `checks or SELF.verify`, so a caller passing none verified one bed's repair
    with another bed's commands -- the shape `Sandbox.root` already carries a warning about."""
    result = harness.GauntletResult()
    with pytest.raises(TypeError):
        harness._run_toolchain(sandbox, result)  # type: ignore[call-arg]


def test_an_undeclared_check_renders_as_skipped_not_failed(harness) -> None:
    """Every Python-only bed read `lint FAIL types FAIL` on every attempt, for two commands
    that were never run -- what `_run_toolchain`'s docstring calls worse than a red row."""
    result = harness.GauntletResult(tests_pass=True, skipped={"lint", "types"})

    assert harness._mark("tests", result) == "tests ok"
    assert harness._mark("lint", result) == "lint skip"
    assert harness._mark("types", result) == "types skip"


def test_a_cut_off_reply_ends_the_loop_instead_of_spending_every_retry(harness) -> None:
    """A retry exists so the model can act on feedback about a bad repair.

    A reply cut off at the token budget carries no such signal: the prompt is unchanged, the
    temperature is 0, and the model scales its deliberation to whatever room it is given --
    measured, 8k against a budget of 8192 and 32.6k against 32768. So three retries buy the
    same truncation three times, at roughly 800 seconds each on the httpx bed.

    Asked by type rather than by message. `oxn.llm.ReplyCutOff` exists so a harness does not
    parse prose to tell "the host was busy" from "the answer was cut in half".
    """
    from oxn.llm import OllamaError, ReplyCutOff

    assert issubclass(ReplyCutOff, OllamaError), "every other model failure is still retryable"
    assert harness._was_cut_off(ReplyCutOff("stopped at the token limit"))
    assert not harness._was_cut_off(OllamaError("Ollama is unreachable"))
    assert not harness._was_cut_off(RuntimeError("something else"))


def test_a_sandbox_under_test_never_lands_in_the_shared_scratch(harness, tmp_path, scratch) -> None:
    """The fixture above is load-bearing, so it is asserted rather than trusted.

    `load_harness` merges every harness module into one namespace, and patching an attribute
    on that namespace rebinds a copy -- `gauntlet.SCRATCH` is what `Sandbox.__init__` reads.
    The tests here therefore wrote into `/tmp/oxn-dogfood`, the same root a live repair run
    uses, and left fourteen directories behind in one suite run.
    """
    source = tmp_path / "src"
    source.mkdir()
    box = harness.Sandbox("probe", root=source, venv=False)

    assert box.path.parent == scratch, f"{box.path} escaped into the shared root"
