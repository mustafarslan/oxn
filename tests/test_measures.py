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
        "repeat": 0,
        "run": "20260910T120000",
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


def test_an_attempt_carrying_a_set_still_reaches_the_log(harness) -> None:
    """`GauntletResult.skipped` is a set, and the log is JSON.

    This failed on the *last* action of a real run: two targets, a model call and a full
    toolchain each, all of it discarded by a `TypeError` at the write. Nothing before that
    point could have caught it -- every test built the row by hand -- which is what running
    the harness for real was worth.

    Handled generally rather than by converting the one field, because the next set added to
    a result would do exactly the same thing.
    """
    import json

    attempt = harness.Attempt(
        "walk", "m.py", 1, False, {"tests_pass": True, "skipped": {"types", "lint"}}
    )
    written = json.dumps({"x": attempt.gauntlet}, default=harness._jsonable)
    assert json.loads(written)["x"]["skipped"] == ["lint", "types"], "sorted, so a diff is stable"


def test_an_unloggable_value_is_refused_rather_than_stringified(harness) -> None:
    """A silent `str(obj)` would put `<object at 0x...>` in a benchmark record.

    The point of the log is that it can be read back and measured. A field that serialises
    to an address is a row that looks present and answers nothing.
    """
    import pytest as _pytest

    with _pytest.raises(TypeError):
        harness._jsonable(object())


def test_a_rate_says_how_many_samples_it_is_over(harness) -> None:
    """One sample per target is an anecdote about a stochastic process, and looks identical.

    `oxn.llm.generate` is temperature 0 and says so in its own docstring: three runs of the
    identical repair prompt returned three materially different rewrites. A convergence rate
    from one sample per cell therefore cannot be distinguished from noise, and it renders
    exactly like one that can -- which is why the count is a column rather than a footnote.
    """
    rows = [_row(repeat=0), _row(repeat=1, target="b"), _row(repeat=1)]
    (result,) = harness.measures(rows)
    assert result.repeats == 2
    assert "reversed outright" in harness.SMALL_SAMPLE_NOTE


def test_rows_written_before_repeats_existed_count_as_one_sample(harness) -> None:
    """The log predates the field, and `.get` defaulting to 0 must not read as zero samples."""
    row = _row()
    del row["repeat"]
    (result,) = harness.measures([row])
    assert result.repeats == 1


def test_repeats_do_not_inflate_the_target_count(harness) -> None:
    """Five samples of one target is one target, five times -- not five targets.

    Counting them as targets would make an arm look broader than it was tested, and would
    divide the convergence rate by the wrong denominator in the flattering direction.
    """
    rows = [_row(repeat=n) for n in range(5)]
    (result,) = harness.measures(rows)
    assert result.targets == 1
    assert result.repeats == 5
    assert result.attempts == 5


def test_one_runs_numbers_do_not_leak_into_another(harness) -> None:
    """The log accumulates, and pooling it reads exactly like a single experiment.

    A bounded grid's control arm reported 50% convergence on the strength of one row from an
    earlier *pilot* -- a run whose target selection was already known to be broken and whose
    numbers had been declared invalid. Nothing in the table could say so, because there was
    nothing in the row that said which run it came from.
    """
    rows = [
        _row(run="20260910T090000", arm="none", target="a", accepted=True),
        _row(run="20260910T120000", arm="none", target="b", accepted=False),
    ]
    (old,) = harness.measures(rows, "20260910T090000")
    (new,) = harness.measures(rows, "20260910T120000")
    assert old.convergence_rate == 1.0
    assert new.convergence_rate == 0.0, "the earlier run must not lift the later one"
    assert len(harness.measures(rows, None)) == 1, "pooling is still possible, just not default"


def test_the_latest_run_is_the_one_reported(harness) -> None:
    """`report` answers "the run I just did" without a separate index to consult."""
    rows = [_row(run="20260910T090000"), _row(run="20260910T120000")]
    assert harness.latest_run(rows) == "20260910T120000"


def test_rows_from_before_run_ids_identify_no_run(harness) -> None:
    """The existing log predates the field, and must not be adopted by the newest run."""
    row = _row()
    del row["run"]
    assert harness.latest_run([row]) == ""
    assert harness.measures([row], "20260910T120000") == []


def test_every_arm_of_one_grid_shares_its_run_id(harness) -> None:
    """The grid is the unit of comparison, so its arms must be one run and not six."""
    grid = harness.Grid(
        bed="self",
        arms=("none", "hybrid"),
        ceiling=6,
        limit=1,
        retries=1,
        repeats=1,
        backend="dry-run",
        run_id="20260910T120000",
    )
    ids = {harness._session(grid, name).run_id for name in grid.arms}
    assert ids == {"20260910T120000"}


def test_the_small_sample_warning_is_grounded_in_a_measured_reversal(harness) -> None:
    """The threshold is a judgement, and the evidence for it is in the note.

    Two runs of one configuration -- same target, same arms, same models, same flags --
    produced opposite orderings: `hybrid` 1/2 against `none` 0/2, then `none` 2/2 against
    `hybrid` 0/2. n=2 is therefore *demonstrably* insufficient here, not arguably so, and a
    reader who is told the number should also be told why it is that number.
    """
    assert harness.ENOUGH_SAMPLES > 2, "n=2 was measured to reverse; the bar must exceed it"
    assert "same flags" in harness.SMALL_SAMPLE_NOTE
    assert "judgement rather than a power analysis" in harness.SMALL_SAMPLE_NOTE


# ---- a reply the model was not allowed to finish -----------------------------------------


def test_a_truncated_attempt_is_counted_apart_from_a_failed_repair() -> None:
    """The confound this would have introduced runs *against* the treatment.

    Found on the first honest attempt the httpx bed produced: `glm-5.3:cloud` wrote 123,471
    characters, hit `num_predict`, and the extracted candidate was a function body ending
    mid-block. The harness filed it as `tests FAIL types FAIL target_present False`, which is
    indistinguishable from a repair that was simply bad.

    Arms differ in prompt size -- the pilot logged `hybrid` at 646,451 characters against
    `none` at 54,543, on one budget -- so the arm carrying more guidance has less room to
    answer and truncates more often. Folding those into "did not converge" would have made
    the treatment look worse for a reason that is about the budget.
    """
    from runlog import TRUNCATION_MARKER, measures

    rows = [
        _row(target="a", accepted=False, error=f"glm {TRUNCATION_MARKER} with ...", run="r1"),
        _row(target="b", accepted=True, run="r1"),
    ]
    (result,) = measures(rows, run="r1")

    assert result.truncated == 1
    assert result.converged == 1, "the truncated attempt is not a failure of the arm"
    assert result.targets == 2


def test_an_ordinary_failure_is_not_counted_as_truncation() -> None:
    from runlog import measures

    rows = [_row(target="a", accepted=False, error="Ollama is unreachable", run="r1")]
    (result,) = measures(rows, run="r1")

    assert result.truncated == 0


def test_a_truncated_attempt_is_not_counted_as_a_retry_either() -> None:
    """`attempts_per_target` would otherwise carry the confound `conv` just lost.

    An arm with a bigger prompt truncates more, and a truncation that counted as an attempt
    would make it read as needing more tries -- the same artifact in the next column along.
    """
    from runlog import measures

    rows = [
        _row(target="a", accepted=False, cut_off=True, run="r1"),
        _row(target="a", accepted=True, run="r1"),
    ]
    (result,) = measures(rows, run="r1")

    assert result.truncated == 1
    assert result.attempts == 1, "the truncated row is not a try the arm needed"
    assert result.attempts_per_target == 1.0
