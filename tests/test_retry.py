"""The bounded remediation loop, and the channel it travels on.

Two things are asserted here, and the first is the reason the second was worth building.

**The payload has to reach the agent.** For the whole of P9, `oxn check --json` wrote its
findings — the increment trail this project calls "the payload is the product" — to
*stdout*, and exited 2. Claude Code's hook contract shows **stderr** to Claude on exit 2 and
relegates stdout to transcript mode, so what a rejected edit actually delivered was the
string `No stderr output`. The gate blocked perfectly and explained nothing. Every test in
the first section fails against that version.

**The loop has to be bounded.** ADR-0003 section 4: LLM refactoring frequently fails to get
under a complexity threshold at all (arXiv 2508.11958), so a gate that keeps saying no to an
agent that keeps saying yes is an infinite loop with a token budget attached. The budget is
counted per `(session, finding)` and the halt is a different message, not a different exit
code — a `PostToolUse` hook runs after the tool and cannot stop a turn, so the only thing
that bounds the loop is telling the agent, in as many words, to stop and escalate.

The subprocess drive is deliberate: `run_check` cannot exercise any of this, because the
session id, the two output streams and the exit code all live in `oxn.cli`'s fast path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from oxn import thresholds
from oxn.check import CheckReport, Finding, remediation
from oxn.config import Config, ConfigError
from oxn.retry import Attempt, charge, ledger_path

#: Cognitive complexity 24 against a ceiling of 12: over by enough that a plausible partial
#: repair still fails, which is what the budget exists to survive.
TANGLED = (
    "def tangled(rows, flag):\n"
    "    total = 0\n"
    "    for row in rows:\n"
    "        if row:\n"
    "            for cell in row:\n"
    "                if cell:\n"
    "                    while cell > 0:\n"
    "                        if flag:\n"
    "                            total += 1\n"
    "                        else:\n"
    "                            total -= 1\n"
    "                        cell -= 1\n"
    "                else:\n"
    "                    total += 2\n"
    "        else:\n"
    "            total -= 2\n"
    "    return total\n"
)
CALM = "def tangled(rows, flag):\n    return len(rows) if flag else 0\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A repository with the default budget and one file the agent keeps failing to fix."""
    (tmp_path / "oxn.yaml").write_text("retry_budget: 3\n")
    (tmp_path / "edited.py").write_text(TANGLED)
    return tmp_path


def hook(directory: Path, *, session: str | None = "s1", path: str = "edited.py"):
    """One `PostToolUse` invocation, as Claude Code makes it.

    Returns `(exit code, parsed stdout, stderr)` — all three, because the whole point of
    these tests is that the three disagree about what the agent is told.
    """
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": path}}
    if session is not None:
        payload["session_id"] = session
    result = subprocess.run(
        [sys.executable, "-m", "oxn.cli", "check", "--json"],
        capture_output=True,
        text=True,
        cwd=directory,
        input=json.dumps(payload),
    )
    return result.returncode, json.loads(result.stdout), result.stderr


# ---- the channel ------------------------------------------------------------------------


def test_a_rejected_edit_explains_itself_on_stderr(project: Path) -> None:
    """The regression test for the defect this work found.

    Not "stderr is non-empty": the increment trail is the thing that was lost, so the
    assertion is that a specific increment, at its specific line, reaches the agent.
    """
    code, _, stderr = hook(project)

    assert code == 2
    assert "cognitive_complexity 24, above the ceiling of 12" in stderr
    assert "+3 at line 5: `for` nested 2 deep" in stderr


def test_the_json_stays_on_stdout_for_ci_and_check_code(project: Path) -> None:
    """stderr is an addition, not a move. Two readers, two shapes, one run."""
    _, payload, stderr = hook(project)

    assert payload["status"] == "FAILED"
    assert payload["violations"][0]["rule"] == "cognitive_complexity"
    assert stderr != ""


def test_a_passing_check_says_nothing_at_all(project: Path) -> None:
    """A hook that speaks when nothing is wrong is a hook people switch off."""
    (project / "edited.py").write_text(CALM)
    code, payload, stderr = hook(project)

    assert (code, payload["status"], stderr) == (0, "PASSED", "")


def test_a_broken_config_reaches_the_person_who_broke_it(project: Path) -> None:
    """Exit 1 shows stderr to the user, and "nothing was checked" is the load-bearing half:
    a gate that cannot start must never be mistaken for a gate that passed."""
    (project / "oxn.yaml").write_text("ceilings:\n  nonsense: 3\n")
    code, payload, stderr = hook(project)

    assert code == 1
    assert payload["status"] == "ERROR"
    assert "nothing was checked" in stderr


# ---- the budget -------------------------------------------------------------------------


def test_the_attempt_count_climbs_across_edits_of_the_same_violation(project: Path) -> None:
    for expected in (1, 2, 3):
        _, payload, stderr = hook(project)
        assert f"[attempt {expected} of 3]" in stderr
        assert payload["retry"]["attempts"][_key()]["count"] == expected


def test_the_budget_is_spent_on_the_attempt_after_the_last_repair(project: Path) -> None:
    """A budget of 3 buys three *repairs*, so the fourth sighting is the one that halts.

    Report *n* means *n - 1* repairs were attempted and none of them worked. Halting on the
    third sighting would spend the budget after two.
    """
    for _ in range(3):
        assert "retry budget spent" not in hook(project)[2]

    code, payload, stderr = hook(project)

    assert code == 2
    assert _key() in payload["retry"]["exhausted"]
    assert "retry budget spent" in stderr
    assert "Stop editing these entities and report to the user" in stderr


def test_the_halt_report_shows_whether_the_agent_was_converging(project: Path) -> None:
    """The trajectory is the point. "24 -> 17 -> 17" tells a human the agent improved once
    and then stalled, which is a different conversation from "24 -> 24 -> 24"."""
    hook(project)
    (project / "edited.py").write_text(TANGLED.replace("                        else:\n", ""))
    for _ in range(3):
        hook(project)

    stderr = hook(project)[2]

    assert "cognitive_complexity 24 -> " in stderr
    assert "(ceiling 12)" in stderr


def test_a_repaired_violation_is_forgotten(project: Path) -> None:
    """Otherwise the budget is a per-session lifetime allowance rather than a retry count:
    an agent that fixed a function and later broke it again would start at the halt."""
    for _ in range(3):
        hook(project)
    (project / "edited.py").write_text(CALM)
    assert hook(project)[0] == 0

    (project / "edited.py").write_text(TANGLED)
    _, payload, stderr = hook(project)

    assert payload["retry"]["attempts"][_key()]["count"] == 1
    assert "[attempt 1 of 3]" in stderr


def test_a_clean_file_does_not_forget_violations_elsewhere(project: Path) -> None:
    """The hook measures one file, so pruning everything it did not see would hand the
    agent a fresh budget for `a.py` every time it edited `b.py`."""
    (project / "other.py").write_text(CALM)
    hook(project)
    hook(project)
    hook(project, path="other.py")

    assert "[attempt 3 of 3]" in hook(project)[2]


def test_two_sessions_do_not_share_a_budget(project: Path) -> None:
    for _ in range(4):
        hook(project, session="first")

    assert "[attempt 1 of 3]" in hook(project, session="second")[2]


def test_without_a_session_there_is_no_budget(project: Path) -> None:
    """A person or a CI job has no session to count against, and halting a build because
    three commits touched the same debt would be nonsense."""
    for _ in range(5):
        code, payload, stderr = hook(project, session=None)

    assert code == 2
    assert "retry" not in payload
    assert "attempt" not in stderr
    assert not (project / ".oxn" / "cache" / "attempts").exists()


def test_a_budget_of_zero_never_halts(project: Path) -> None:
    (project / "oxn.yaml").write_text("retry_budget: 0\n")
    for _ in range(5):
        code, payload, stderr = hook(project)

    assert code == 2
    assert "retry" not in payload
    assert "retry budget spent" not in stderr


# ---- the ledger -------------------------------------------------------------------------


def test_a_session_id_cannot_name_a_path_of_its_own_choosing(tmp_path: Path) -> None:
    """The id arrives from an external payload and becomes a filename."""
    path = ledger_path(tmp_path, "../../etc/passwd")

    assert path.parent == tmp_path / ".oxn" / "cache" / "attempts"
    assert path.name == ".._.._etc_passwd.json"


def test_a_corrupt_ledger_costs_an_attempt_rather_than_the_gate(tmp_path: Path) -> None:
    """Failing a hook because a JSON file was truncated by a crash breaks the gate itself,
    which is a far worse trade than forgetting one retry count."""
    path = ledger_path(tmp_path, "s1")
    path.parent.mkdir(parents=True)
    path.write_text("{ this is not json")

    assert charge(path, {"a.py": {"k": 3.0}}) == {"k": Attempt(count=1, values=(3.0,))}


def test_the_trajectory_is_capped(tmp_path: Path) -> None:
    """A long session must not grow one entry without bound."""
    path = ledger_path(tmp_path, "s1")
    for value in range(20):
        attempts = charge(path, {"a.py": {"k": float(value)}})

    assert attempts["k"].count == 20
    assert len(attempts["k"].values) == 8
    assert attempts["k"].values[-1] == 19.0


def test_stale_session_files_are_pruned(tmp_path: Path) -> None:
    import os

    from oxn.retry import MAX_LEDGER_AGE_S

    stale = ledger_path(tmp_path, "old")
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")
    ancient = stale.stat().st_mtime - MAX_LEDGER_AGE_S - 1
    os.utime(stale, (ancient, ancient))

    charge(ledger_path(tmp_path, "new"), {"a.py": {}})

    assert not stale.exists()


# ---- what counts as an attempt ----------------------------------------------------------


def test_a_regression_is_charged_like_any_other_failure() -> None:
    """`CheckReport.blocking` excludes `regressed`, and a baselined violation the agent
    keeps making worse is exactly what a retry budget is for."""
    worse = Finding("cognitive_complexity", "a.py", "a.f", 1, 30.0, 12.0)
    report = CheckReport(
        regressed=[worse],
        attempts={worse.key: Attempt(count=4, values=(20.0, 30.0))},
        retry_budget=3,
    )

    assert report.failing == [worse]
    assert report.exhausted == [worse]
    assert "retry budget spent" in remediation(report)


def test_an_advisory_finding_never_spends_the_budget() -> None:
    """ADR-0002: an unsound measurement is reported, never enforced. Charging an attempt
    for one would let a measurement that cannot block halt the agent anyway."""
    unsound = Finding("cognitive_complexity", "a.py", "a.f", 1, 30.0, 12.0, blocking=False)
    report = CheckReport(findings=[unsound], retry_budget=3)

    assert report.failing == []
    assert report.exhausted == []


# ---- the configured number ----------------------------------------------------------------


def test_the_budget_defaults_to_the_documented_threshold(tmp_path: Path) -> None:
    assert Config.defaults(tmp_path).retry_budget == thresholds.RETRY_BUDGET


@pytest.mark.parametrize("value", ["3", 1.5, -1, True])
def test_a_budget_that_is_not_a_count_is_rejected_rather_than_coerced(
    tmp_path: Path, value: object
) -> None:
    """`True` is the one that matters: it is an `int` in Python, so a silent coercion gives
    a budget of one — a working configuration that means nothing anybody intended."""
    (tmp_path / "oxn.yaml").write_text(f"retry_budget: {json.dumps(value)}\n")

    with pytest.raises(ConfigError, match="retry_budget"):
        Config.load(tmp_path)


def test_an_explicitly_empty_budget_means_the_default(tmp_path: Path) -> None:
    """`retry_budget:` with nothing after it is YAML for `None`, and a key someone started
    writing and left blank means "as it comes", not "no bound"."""
    (tmp_path / "oxn.yaml").write_text("retry_budget:\n")

    assert Config.load(tmp_path).retry_budget == thresholds.RETRY_BUDGET


def test_the_calibration_surface_lists_the_budget() -> None:
    """`oxn calibration` claims to list *every* tunable. A number that hides in a module is
    exactly the folklore that surface exists to prevent."""
    from oxn.calibration import Evidence, parameters

    budget = next(p for p in parameters() if p.name == "RETRY_BUDGET")

    assert budget.value == float(thresholds.RETRY_BUDGET)
    # Repair trajectories back its *cost*, not its value: `observations` counts what
    # has been seen at the current setting, and `evidence` carries whether it was fitted.
    #
    # The count itself is pinned by `test_the_retry_budget_counts_the_trajectories_it_was
    # _observed_on`, against the log rather than against a literal. It was written here too,
    # and both had to be edited whenever the harness ran -- so the second copy was a number
    # that could disagree with the log while still passing, which is the failure this whole
    # surface exists to prevent.
    assert budget.observations > 0
    assert budget.evidence is Evidence.JUDGEMENT


def test_the_readme_quotes_what_the_hook_actually_says() -> None:
    """The README shows both messages as sample output, and sample output that has drifted
    from the program is worse than none: it is the documentation people trust most."""
    stuck = Finding("cognitive_complexity", "a.py", "a.f", 6, 24.0, 12.0)
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()

    live = remediation(CheckReport(findings=[stuck], retry_budget=3))
    halt = remediation(
        CheckReport(
            findings=[stuck],
            attempts={stuck.key: Attempt(count=4, values=(24.0, 17.0, 17.0, 17.0))},
            retry_budget=3,
        )
    )

    assert live.splitlines()[0] in readme
    assert halt.splitlines()[0] in readme
    assert "cognitive_complexity 24 -> 17 -> 17 -> 17 (ceiling 12)" in readme


def _key() -> str:
    return "cognitive_complexity|edited.py|edited.tangled"


def dogfood_rows() -> list[dict]:
    """Every attempt `scripts/dogfood.py` has logged, oldest first.

    Shared by the two tests below rather than private to either: they assert different claims
    about the same file -- how many trajectories it holds, and where the repairs landed.
    """
    log = Path(__file__).resolve().parent.parent / "benchmarks" / "dogfood-log.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def test_the_retry_budget_counts_the_trajectories_it_was_observed_on() -> None:
    """A trajectory is a run beginning at `attempt == 1`, and that definition is a correction.

    The log grew through seven row shapes, and `run`, `arm` and `repeat` are absent from 35 of
    its 44 rows -- grouping on them collapsed unlike rows into one trajectory and undercounted
    these as 16. Every row has carried `attempt` since the first one.
    """
    from oxn.calibration import parameters

    budget = next(parameter for parameter in parameters() if parameter.name == "RETRY_BUDGET")
    starts = [row for row in dogfood_rows() if row["attempt"] == 1]

    assert budget.observations == len(starts)


def test_no_repair_that_worked_ever_needed_a_third_attempt() -> None:
    """The claim in `RETRY_BUDGET`'s provenance, pinned to the file it was measured from.

    An accepted attempt *is* a landing, so no grouping is needed to say which attempt each
    repair arrived on. Five successes is why the parameter stays provisional even though the
    observation points the right way.

    The trajectories come from `benchmarks/dogfood-log.jsonl` and not from the hook ledger,
    which cannot supply them: `charge` rewrites each path with only what the current run
    found, so a violation is forgotten at the moment it is repaired -- exactly the event a fit
    needs. An earlier `fit_when` claimed otherwise.
    """
    from oxn.calibration import parameters

    budget = next(parameter for parameter in parameters() if parameter.name == "RETRY_BUDGET")
    landed = sorted(row["attempt"] for row in dogfood_rows() if row["accepted"])

    assert landed == [1, 1, 1, 1, 2]
    assert budget.is_provisional


def test_many_helpers_counts_the_extractions_it_was_observed_on() -> None:
    """An *attempt* is not a labelled extraction, and the gap between them is the finding.

    Most attempts never extract anything: they fail tests, lint or types first, or they
    simplify the target without introducing a helper. Counting attempts as labels is what made
    this parameter look close to fittable when it is not -- 44 attempts, 4 extractions, 1 of
    them judged. This pins the claim to the file so the two cannot drift apart.
    """
    from oxn.calibration import parameters

    extractions = [row for row in dogfood_rows() if (row.get("gauntlet") or {}).get("new_helpers")]
    budget = next(parameter for parameter in parameters() if parameter.name == "MANY_HELPERS")

    assert budget.observations == len(extractions)
    assert budget.is_provisional


# ---- what the ledger forgets, and the record that keeps it ------------------------------


def test_a_repair_is_recorded_with_the_attempts_it_took(tmp_path) -> None:
    """The event the ledger drops, which is the one the retry budget needs to be fitted.

    `charge` rewrites each path with only what the current run found, so a violation that gets
    fixed leaves no trace at the moment it is fixed -- and "of the repairs that eventually
    succeed, how many attempts did they need" is a question about exactly those events.
    """
    from oxn.retry import charge, ledger_path, repairs_path

    ledger, repairs = ledger_path(tmp_path, "s"), repairs_path(tmp_path)
    key = "cognitive_complexity|a.py|a.f"
    charge(ledger, {"a.py": {key: 27.0}}, repairs)
    charge(ledger, {"a.py": {key: 19.0}}, repairs)
    charge(ledger, {"a.py": {}}, repairs)

    recorded = [json.loads(line) for line in repairs.read_text().splitlines()]
    assert [(r["rule"], r["attempts"], r["values"]) for r in recorded] == [
        ("cognitive_complexity", 2, [27.0, 19.0])
    ]


def test_a_run_that_repairs_nothing_writes_nothing(tmp_path) -> None:
    """The common path must not pay for the rare one.

    ADR-0002 gives the hook 200 ms p95 on every edit, and almost every edit repairs nothing.
    An append-only record that appended on every run would spend that budget to learn nothing.
    """
    from oxn.retry import charge, ledger_path, repairs_path

    ledger, repairs = ledger_path(tmp_path, "s"), repairs_path(tmp_path)
    key = "cognitive_complexity|a.py|a.f"
    charge(ledger, {"a.py": {key: 27.0}}, repairs)
    charge(ledger, {"a.py": {key: 27.0}}, repairs)

    assert not repairs.exists()


def test_a_file_that_was_not_measured_is_not_a_repair(tmp_path) -> None:
    """ "Gone" has to mean measured-and-clean, not merely absent from this run.

    `charge`'s contract already draws that line -- paths absent from `measured` keep whatever
    the ledger holds -- and a repair record that ignored it would count every narrow hook run
    as having fixed everything it did not look at.
    """
    from oxn.retry import charge, ledger_path, repairs_path

    ledger, repairs = ledger_path(tmp_path, "s"), repairs_path(tmp_path)
    charge(ledger, {"a.py": {"cognitive_complexity|a.py|a.f": 27.0}}, repairs)
    charge(ledger, {"b.py": {}}, repairs)

    assert not repairs.exists()


def test_the_record_outlives_the_ledgers_it_came_from(tmp_path) -> None:
    """It accumulates across sessions; the ledgers expire on a seven-day timer.

    Session state and evidence are opposite kinds of file, so the record lives outside
    `LEDGER_DIR` rather than trusting `_prune`'s glob to keep missing it.
    """
    from oxn.retry import LEDGER_DIR, charge, ledger_path, repairs_path

    ledger, repairs = ledger_path(tmp_path, "s"), repairs_path(tmp_path)
    key = "cognitive_complexity|a.py|a.f"
    charge(ledger, {"a.py": {key: 27.0}}, repairs)
    charge(ledger, {"a.py": {}}, repairs)

    assert repairs.exists()
    assert LEDGER_DIR not in repairs.parents
    assert not str(repairs).startswith(str(tmp_path / LEDGER_DIR))


def test_an_unwritable_record_does_not_fail_the_hook(tmp_path) -> None:
    """Losing a data point costs a calibration sample; failing here costs the gate."""
    from oxn.retry import charge, ledger_path

    ledger = ledger_path(tmp_path, "s")
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    key = "cognitive_complexity|a.py|a.f"

    charge(ledger, {"a.py": {key: 27.0}}, blocked / "repairs.jsonl")
    attempts = charge(ledger, {"a.py": {}}, blocked / "repairs.jsonl")

    assert attempts == {}
