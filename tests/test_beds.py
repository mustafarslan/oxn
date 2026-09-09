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


def test_an_unfetched_bed_says_the_one_command_that_fixes_it(harness) -> None:
    """The pins exist; the checkout does not. That is a fetch, not a decision."""
    with pytest.raises(SystemExit) as raised:
        harness.bed("slop-code-bench")
    message = str(raised.value)
    assert "not fetched" in message
    assert "fetch_corpora.py --use eval" in message


def test_a_bed_with_no_verification_is_refused_rather_than_guessed_at(harness, tmp_path) -> None:
    """The refusal that matters: a repair nothing checks is worse than no repair.

    Its message carries *why* rather than only *what*, because the fix is a decision made
    with the corpus open -- SlopCodeBench verifies by its own per-problem checkpoints -- and
    a reader who is only told "no verify" would reach for a plausible default.
    """
    declared = harness.BEDS["slop-code-bench"]
    assert not declared.verify
    assert declared.blocked_on and "checkpoints" in declared.blocked_on

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


def test_the_manifest_pins_every_bed_that_needs_fetching(harness) -> None:
    """The declared beds and the pinned corpora must be the same set, or one of them lies.

    A bed the manifest does not pin can never be fetched; a `use: eval` pin no bed names is
    a download nothing consumes -- which is what these two were until now.
    """
    import re
    from pathlib import Path

    manifest = (Path(harness.ROOT) / "benchmarks" / "manifest.yaml").read_text()
    pinned = {
        name
        for name, block in re.findall(r"- name: (\S+)\n(.*?)(?=\n  - name:|\Z)", manifest, re.S)
        if "use: eval" in block
    }
    declared = {name for name, found in harness.BEDS.items() if found.corpus}
    assert declared == pinned, f"declared {sorted(declared)} vs pinned {sorted(pinned)}"
