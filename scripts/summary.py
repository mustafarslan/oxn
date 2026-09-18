"""What the dogfood log says, separately from producing it.

Reading the log and reporting on it has nothing to do with running a repair -- no sandbox,
no model, no subprocess. Splitting it out is the same seam that already separates
`gauntlet.py` (deterministic verification) from `actor.py` (the model-facing half), and it
is the seam `oxn check` asked for: `dogfood.py` crossed its own file ceiling.

The numbers here are the study arXiv 2508.11958 asks for -- does an LLM refactoring loop
converge, and how often -- so the reporting is deliberately conservative about what counts
as a result. A score measured on code that does not work is not a result.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from console import BOLD, DIM, GREEN, RED, RESET, say

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "benchmarks" / "dogfood-log.jsonl"


def summarise() -> None:
    """What the log says about convergence -- the study 2508.11958 asks for."""
    if not LOG.exists():
        say("no attempts logged yet")
        return
    rows = [json.loads(line) for line in LOG.read_text().splitlines() if line.strip()]
    by_target: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_target.setdefault(row["target"], []).append(row)

    converged = sum(1 for group in by_target.values() if any(r["accepted"] for r in group))
    say(f"{BOLD}{len(by_target)} target(s), {len(rows)} attempt(s){RESET}")
    say(f"  converged: {converged}/{len(by_target)}")
    for name, group in sorted(by_target.items()):
        say(_target_line(name, group))

    agreement = _judge_agreement(rows)
    if agreement:
        say(agreement)
    _arm_table(rows)


def _arm_table(rows: list[dict[str, Any]]) -> None:
    """P11's measures, one line per (arm, backend), worst convergence first.

    Printed only when more than one configuration is present. A table of one row is not a
    comparison, and putting one on screen invites reading a single arm's convergence rate as
    though it meant something on its own -- the control is what makes it mean anything.
    """
    from runlog import (
        COST_NOTE,
        ENOUGH_SAMPLES,
        SMALL_SAMPLE_NOTE,
        TRUNCATION_NOTE,
        latest_run,
        measures,
    )

    run = latest_run(rows)
    found = measures(rows, run or None)
    if len(found) < 2:
        return
    scope = f"run {run}" if run else "every attempt ever logged, which is rarely the question"
    say(f"\n{BOLD}arms{RESET} {DIM}({scope}){RESET}")
    say(
        f"  {'arm':16s}{'backend':12s}{'n':>3s}{'conv':>6s}{'att/t':>7s}"
        f"{'ok':>4s}{'cut':>5s}{'chars':>9s}"
    )
    for result in found:
        say(
            f"  {result.arm:16s}{result.backend:12s}{result.repeats:>3d}"
            f"{result.convergence_rate:>5.0%} {result.attempts_per_target:>6.1f}"
            f"{result.correct:>4d}{result.truncated:>5d}"
            f"{result.prompt_chars + result.reply_chars:>9,d}"
        )
    if any(result.repeats < ENOUGH_SAMPLES for result in found):
        say(f"  {DIM}{SMALL_SAMPLE_NOTE}{RESET}")
    if any(result.truncated for result in found):
        say(f"  {DIM}{TRUNCATION_NOTE}{RESET}")
    say(f"  {DIM}{COST_NOTE}{RESET}")


def _target_line(name: str, group: list[dict[str, Any]]) -> str:
    """One target's trajectory: where it started, the best working result, and the verdict."""
    scored = [row for row in group if row["gauntlet"]]
    best = _best_working_score(scored)
    first = _first_measured(scored)
    status = f"{GREEN}accepted{RESET}" if any(r["accepted"] for r in group) else f"{RED}no{RESET}"
    errors = sum(1 for row in group if row.get("error"))
    note = f"  ({errors} failed to run)" if errors else ""
    return f"  {name:32} {_num(first)} -> {_num(best)}  attempts={len(group)}  {status}{note}"


def _first_measured(scored: list[dict[str, Any]]) -> float | None:
    """Where this target started, from the first row that actually measured it.

    **`UNMEASURABLE` is a sentinel, not a score**, and reading it as one made `urlparse` print
    `999 -> 58` for a function that scores 63. This comment used to say "take the first row
    that has a measurement rather than the first row" while taking the first row's number
    whatever it was -- the intent was right and the sentinel was not recognised as the
    absence it stands for.

    Those rows are in the log because they were honestly recorded before the bug behind them
    was found: `measure` was resolving an external bed's path against this repository, so the
    before-state of every corpus target came back unmeasurable. The rows stay. What changes
    is that a reader is no longer told 999 was a complexity score.
    """
    from gauntlet import UNMEASURABLE

    return next(
        (
            row["gauntlet"]["score_before"]
            for row in scored
            if row["gauntlet"].get("score_before") not in (None, UNMEASURABLE)
        ),
        None,
    )


def _best_working_score(scored: list[dict[str, Any]]) -> float | None:
    """The lowest score among attempts whose code actually works.

    A score only counts as a result if the code it was measured on passes its tests and
    still contains the function. Attempts that fail, or that deleted the target outright,
    still produce a number -- and it is usually a flatteringly low one.
    """
    valid = [
        row
        for row in scored
        if row["gauntlet"].get("tests_pass") and row["gauntlet"].get("target_present", True)
    ]
    from gauntlet import UNMEASURABLE

    return min(
        (
            row["gauntlet"]["score_after"]
            for row in valid
            if row["gauntlet"].get("score_after") != UNMEASURABLE
        ),
        default=None,
    )


def _judge_agreement(rows: list[dict[str, Any]]) -> str:
    """How often the judge and the deterministic gauntlet reached the same verdict.

    The calibration signal for the whole actor/judge design, so it is reported even when
    the sample is tiny -- with the denominator visible, which is the honest way to show n=1.
    """
    judged = [row for row in rows if row.get("judge", {}).get("verdict") in {"accept", "reject"}]
    if not judged:
        return ""
    agree = sum(
        1
        for row in judged
        if (row["judge"]["verdict"] == "accept") == bool(row["gauntlet"].get("tests_pass"))
    )
    return f"\n  judge/gauntlet agreement: {agree}/{len(judged)}"


def _num(value: float | None) -> str:
    return "--" if value is None else f"{value:g}"
