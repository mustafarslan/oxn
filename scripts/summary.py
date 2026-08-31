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

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "benchmarks" / "dogfood-log.jsonl"

DIM, GREEN, RED, BOLD, RESET = "\033[2m", "\033[32m", "\033[31m", "\033[1m", "\033[0m"


def say(message: str = "") -> None:
    print(message)


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


def _target_line(name: str, group: list[dict[str, Any]]) -> str:
    """One target's trajectory: where it started, the best working result, and the verdict."""
    scored = [row for row in group if row["gauntlet"]]
    best = _best_working_score(scored)
    # The first attempt may have died before measuring anything, so take the first row that
    # has a measurement rather than the first row.
    first = scored[0]["gauntlet"].get("score_before") if scored else None
    status = f"{GREEN}accepted{RESET}" if any(r["accepted"] for r in group) else f"{RED}no{RESET}"
    errors = sum(1 for row in group if row.get("error"))
    note = f"  ({errors} failed to run)" if errors else ""
    return f"  {name:32} {_num(first)} -> {_num(best)}  attempts={len(group)}  {status}{note}"


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
    return min((row["gauntlet"]["score_after"] for row in valid), default=None)


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
