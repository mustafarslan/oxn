"""Where the harness takes targets from, and what it refuses to do without.

The harness repaired OXN's own functions and nothing else -- `src/oxn` was written into
`select_targets`. That is a fine bed and a bad only-bed: a result measured solely on the
repository the tool was written for is a statement about that repository, which is exactly
why P11 names three.

The interesting behaviour here is the refusing. `benchmarks/manifest.yaml` has pinned
SlopCodeBench and Conduit as `use: eval` all along, so pointing at them is easy and running
them is not: a bed needs verification commands, and inventing a test command for a corpus
nobody has opened produces a harness that runs, reports, and checks nothing. That is the
failure this project keeps finding in its own metrics, so it is a hard error with the reason.
"""

from __future__ import annotations

import pytest
from tests.test_dogfood import load_harness


@pytest.fixture(scope="module")
def harness():
    return load_harness()


def test_the_self_bed_is_runnable_and_is_this_repository(harness) -> None:
    """The bed every result so far was measured on, and the only one needing no fetch."""
    found = harness.SELF
    assert found.fetched and found.runnable
    assert found.sources == ("src/oxn",)
    assert (found.root / "src" / "oxn").is_dir()
    assert found.verify, "a bed that cannot verify cannot report"


def test_a_fetched_bed_stops_asking_to_be_fetched(harness) -> None:
    """Both eval beds are pinned in the lock and on disk, so the refusal moves on.

    It moves to the *right* refusal: fetching them was the easy half, and opening them is
    what showed the hard half -- neither is a tree of code this loop can repair.
    """
    for name in ("slop-code-bench", "realworld-conduit"):
        assert harness.BEDS[name].fetched, f"{name} is not fetched; run fetch_corpora --use eval"
        with pytest.raises(SystemExit) as raised:
            harness.bed(name)
        assert "no verification commands" in str(raised.value)


def test_an_unfetched_bed_would_say_the_one_command_that_fixes_it(harness) -> None:
    """The other refusal, exercised on a bed that cannot be on disk."""
    absent = harness.Bed(name="not-here", corpus="not-here", sources=())
    assert not absent.fetched
    harness.BEDS["not-here"] = absent
    try:
        with pytest.raises(SystemExit) as raised:
            harness.bed("not-here")
        assert "not fetched" in str(raised.value)
        assert "fetch_corpora.py --use eval" in str(raised.value)
    finally:
        del harness.BEDS["not-here"]


def test_a_bed_with_no_verification_is_refused_rather_than_guessed_at(harness, tmp_path) -> None:
    """The refusal that matters: a repair nothing checks is worse than no repair.

    Its message carries *why* rather than only *what*, because the fix is a decision made
    with the corpus open -- SlopCodeBench verifies by its own per-problem checkpoints -- and
    a reader who is only told "no verify" would reach for a plausible default.
    """
    declared = harness.BEDS["slop-code-bench"]
    assert not declared.verify
    assert "agent-evaluation harness" in declared.blocked_on, "the reason must be the real one"

    conduit = harness.BEDS["realworld-conduit"]
    assert not conduit.verify
    assert "no implementation" in conduit.blocked_on
    assert "live server" in conduit.blocked_on, "the structural half must be recorded too"

    fetched = harness.Bed(
        name="fetched-but-unverifiable", corpus="", sources=("src",), blocked_on="nothing decided"
    )
    assert fetched.fetched, "the repository root always exists"
    assert not fetched.runnable, "fetched is not enough; it must be checkable"


def test_an_unknown_bed_is_refused_with_the_list(harness) -> None:
    with pytest.raises(SystemExit) as raised:
        harness.bed("slop-code-bnch")
    assert "slop-code-bnch" in str(raised.value) and "slop-code-bench" in str(raised.value)


def test_every_declared_bed_says_why_it_is_a_bed(harness) -> None:
    """A bed with no rationale is a corpus someone happened to have.

    P11's beds were each chosen for a reason -- SlopCodeBench measures the erosion signals
    OXN computes natively, Conduit is the constraint-decay study's own setup -- and the
    reason is what makes a result comparable to the literature it cites.
    """
    for name, found in harness.BEDS.items():
        assert found.why, f"{name} has no rationale"


def test_every_bed_is_pinned_and_every_eval_pin_is_a_bed(harness) -> None:
    """Two directions, and each catches a different lie.

    A bed the manifest does not pin can never be fetched. And a `use: eval` pin that no bed
    names is a download nothing consumes -- which is exactly what SlopCodeBench and Conduit
    were until they became beds.

    The five OSS beds are pinned `use: threshold`, because they are the same repositories
    the ceilings were calibrated against. That is a feature: P11 asks for "real layered OSS
    repos" as a bed, and these are already chosen, already argued for, and already on disk.
    """
    import re
    from pathlib import Path

    manifest = (Path(harness.ROOT) / "benchmarks" / "manifest.yaml").read_text()
    blocks = dict(re.findall(r"- name: (\S+)\n(.*?)(?=\n  - name:|\Z)", manifest, re.S))
    declared = {name for name, found in harness.BEDS.items() if found.corpus}

    assert declared <= set(blocks), f"unpinned beds: {sorted(declared - set(blocks))}"
    eval_pins = {name for name, block in blocks.items() if "use: eval" in block}
    assert eval_pins <= declared, f"eval pins nothing consumes: {sorted(eval_pins - declared)}"


def test_there_is_a_runnable_bed_for_every_supported_language(harness) -> None:
    """Six languages in the goal, six runnable beds, one per language plus this repository.

    A result measured only on Python would say nothing about the five languages whose
    metrics were fixed today, and the beds are what stop that.
    """
    runnable = {name for name, found in harness.BEDS.items() if found.runnable}
    assert {"self", "go-kit", "rust-ripgrep", "python-httpx", "typescript-nest"} <= runnable
    assert "java-spring-petclinic" in runnable


def test_a_bed_declares_its_own_toolchain_and_not_this_projects(harness) -> None:
    """`go vet` is not `ruff`, and a Go module has no type checker to run.

    The gauntlet ran this project's three commands against whatever it was given. That was
    invisible with one bed and would have graded a Rust repair with Python's linter.
    """
    go = harness.BEDS["go-kit"]
    assert ("tests", "go", "test", "./...") in go.verify
    assert not any("ruff" in command for _label, *command in go.verify)
    assert not go.venv, "a Go module needs no virtualenv"
    assert "types" not in {label for label, *_ in go.verify}, "and no type checker"


def test_the_sandbox_copies_the_bed_and_not_always_this_repository(harness, tmp_path) -> None:
    """The defect a second bed would have hit silently: measure one tree, verify another.

    `Sandbox` copied `ROOT` unconditionally while `select_targets` also hardcoded `src/oxn`,
    so the two agreed and nothing showed. Point the harness at a different bed and the
    repair would have been graded against OXN's tests. It is unreachable today because every
    other bed refuses -- and "it can only fail to fire" is not a property to rely on in
    something whose output is a measurement.
    """
    elsewhere = tmp_path / "other-repo"
    elsewhere.mkdir()
    box = harness.Sandbox("probe", root=elsewhere)
    assert box.root == elsewhere
    assert harness.Sandbox("probe").root == harness.ROOT, "the default is still this repository"


@pytest.mark.parametrize(
    ("bed_name", "suffix"),
    (
        ("go-kit", ".go"),
        ("rust-ripgrep", ".rs"),
        ("typescript-nest", ".ts"),
        ("java-spring-petclinic", ".java"),
        ("python-httpx", ".py"),
    ),
)
def test_a_bed_finds_its_own_files_and_not_this_repositorys(harness, bed_name, suffix) -> None:
    """Targets and their explanation must both come from the bed. Neither did.

    `select_targets` found go-kit's files correctly and `_trail_for` then read
    `ROOT / target.path`, so explaining a Go target tried to open a Go file inside this
    repository and raised. `plan` took no bed at all and listed OXN's own functions under
    `--bed go-kit` -- a wrong answer rather than a crash, which is the worse of the two.

    Both were found by running the command, not by reading it. This runs it.
    """
    found = harness.bed(bed_name)
    picked = harness.select_targets(ceiling=3, limit=2, skip=set(), where=found)
    assert picked, f"{bed_name}: nothing over the ceiling, so this proves nothing"
    for target in picked:
        assert target.path.endswith(suffix), f"{bed_name} returned {target.path}"
        assert (found.root / target.path).is_file(), "the path must exist inside the bed"
        assert target.trail, "an explanation read from the wrong root would have raised"
