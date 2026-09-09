"""P11's measures, computed from a run log rather than from a model.

The arms are only worth running if the log they produce can be split by arm and turned into a
comparison. That is what this checks, and it needs no model: the log is JSON, and the measures
are arithmetic over it.

P11 names six measures. Four are computed here -- convergence rate, retry counts, cost, and
functional correctness -- and the two that are not are named rather than quietly dropped:
erosion and verbosity trajectory need a repository measured over time, which is `oxn volume`
across a run rather than this log.
"""

from __future__ import annotations

import json

import pytest
from tests.test_dogfood import load_harness


@pytest.fixture(scope="module")
def harness():
    return load_harness()


def _row(**over):
    """One logged attempt, with the fields the measures read."""
    row = {
        "target": "walk",
        "path": "m.py",
        "attempt": 1,
        "accepted": False,
        "gauntlet": {"tests_pass": False},
        "arm": "hybrid",
        "backend": "ollama",
        "prompt_chars": 100,
        "reply_chars": 50,
        "seconds": 1.0,
    }
    return {**row, **over}


def test_a_target_repaired_on_the_third_try_converged_once(harness) -> None:
    """Convergence is per target, not per accepted attempt.

    Counting attempts would reward an arm for needing more of them, which inverts the
    measure: an arm that flailed twice and then succeeded would outscore one that succeeded
    immediately.
    """
    rows = [
        _row(attempt=1),
        _row(attempt=2),
        _row(attempt=3, accepted=True, gauntlet={"tests_pass": True}),
    ]
    (result,) = harness.measures(rows)
    assert result.targets == 1
    assert result.converged == 1
    assert result.convergence_rate == 1.0
    assert result.attempts == 3
    assert result.attempts_per_target == 3.0


def test_arms_are_reported_separately(harness) -> None:
    """The whole point. One pile of attempts is not an experiment."""
    rows = [
        _row(arm="none", target="a"),
        _row(arm="hybrid", target="a", accepted=True, gauntlet={"tests_pass": True}),
    ]
    found = {r.arm: r for r in harness.measures(rows)}
    assert set(found) == {"none", "hybrid"}
    assert found["none"].convergence_rate == 0.0
    assert found["hybrid"].convergence_rate == 1.0


def test_one_backend_does_not_absorb_another(harness) -> None:
    """A weaker model's failures must not be filed under a stronger one's arm.

    P11 makes model strength a factor -- the capability-equalizer hypothesis is that OXN
    helps weaker models most -- so backend is part of the key, not a note beside it.
    """
    rows = [_row(backend="ollama"), _row(backend="claude-code")]
    assert len(harness.measures(rows)) == 2


def test_functional_correctness_is_measured_not_inferred_from_acceptance(harness) -> None:
    """ "The gate does not break the code" cannot be shown by asking the gate.

    An accepted repair whose tests failed is exactly the finding P11 wants visible, so the
    two numbers are counted separately and are allowed to disagree.
    """
    rows = [_row(accepted=True, gauntlet={"tests_pass": False})]
    (result,) = harness.measures(rows)
    assert result.converged == 1
    assert result.correct == 0, "acceptance must not imply a passing test run"


def test_attempts_logged_before_the_arms_are_not_filed_under_one(harness) -> None:
    """The existing log has 25 such rows, from a different experiment.

    Folding them into `hybrid` -- which is what the harness did then -- would put real
    numbers from another question into its row, and nothing downstream could tell.
    """
    rows = [_row(arm="", backend=""), _row(arm="hybrid")]
    found = {r.arm for r in harness.measures(rows)}
    assert found == {"(unrecorded)", "hybrid"}


def test_cost_is_reported_with_what_it_is(harness) -> None:
    """Characters, not tokens, and the note says so wherever they are printed."""
    rows = [_row(prompt_chars=1000, reply_chars=200)]
    (result,) = harness.measures(rows)
    assert (result.prompt_chars, result.reply_chars) == (1000, 200)
    assert "not tokens" in harness.COST_NOTE


def test_the_real_log_still_parses(harness) -> None:
    """The committed log predates every field above; reading it must not need a migration."""
    rows = harness.read_log()
    assert rows, "benchmarks/dogfood-log.jsonl is empty"
    found = harness.measures(rows)
    assert found and all(r.targets > 0 for r in found)
    assert json.dumps([r.as_dict() for r in found])
