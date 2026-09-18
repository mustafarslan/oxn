"""What a run records, so that one arm can be compared with another.

The repair loop produces attempts; this is what an attempt *is* and where it goes. It moved
out of `scripts/dogfood.py` when that file hit its own `file_sloc` ceiling, and the split was
real rather than a trim: nothing here decides anything, and P11's measures read this rather
than the loop.

**Provenance is the point.** Before the arms existed the log was one undifferentiated pile,
which was fine with one configuration and useless the moment there were twelve -- six arms
against two real backends. An attempt that cannot say which arm produced it cannot enter an
arm table, and a table assembled from such a log would be six copies of one number.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "benchmarks" / "dogfood-log.jsonl"

#: Written by the loop so the log line reads the same however it is produced. `dogfood`
#: re-exports these; nothing else should reach past this module for them.
DIM = "\033[2m"
RESET = "\033[0m"


def say(message: str = "") -> None:
    print(message)


@dataclass
class Attempt:
    """One repair attempt, logged whether it succeeded or not."""

    target: str
    path: str
    attempt: int
    accepted: bool
    gauntlet: dict[str, Any]
    judge: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    #: The endpoint refused to serve this, rather than serving something unusable. 429 above
    #: all, which is a cloud model throttling and says "retry later" in as many words. It
    #: stops the run rather than annotating it: every remaining target would ask the same
    #: endpoint the same question and get the same refusal, and one run did exactly that --
    #: 39 of 44 attempts were identical rate-limit rows against twelve targets nobody looked
    #: at. A log full of those is not a record of a model that cannot refactor.
    endpoint_unavailable: bool = False
    seconds: float = 0.0
    #: The extracted candidate and the raw reply it came from. A few KB each, and the only
    #: way to diagnose a bug in extraction after the fact -- which is precisely the analysis
    #: that was impossible when only the verdict was recorded.
    candidate: str = ""
    reply: str = ""
    #: What to tell the actor if there is another attempt. Carried on the attempt rather
    #: than returned separately, so the loop cannot forget to thread it through.
    next_feedback: str = ""
    #: Which arm and which actor produced this. Without them the log is one undifferentiated
    #: pile and no arm can be compared to any other -- which is the entire experiment.
    arm: str = ""
    backend: str = ""
    #: Which invocation produced it. Without this the log pools every experiment ever run:
    #: a grid's `none` arm read 50% convergence entirely on the strength of a *pilot* row
    #: from a run whose target selection was already known to be broken, and nothing in the
    #: table could say so. An invalidated run must stop contributing, and a table must be
    #: attributable to one configuration.
    run: str = ""
    #: True when the model stopped at its token budget with an answer half written. A field
    #: rather than only a substring of `error`, so a reader of the log does not have to match
    #: prose -- `TRUNCATION_MARKER` stays for rows written before this existed.
    cut_off: bool = False
    #: Which repeat of this (arm, target) produced it. `generate` is temperature 0 and
    #: explicitly not deterministic -- `oxn.llm` records three materially different rewrites
    #: from one prompt -- so a convergence rate over a single sample per target is not
    #: distinguishable from noise, and the measures say how many samples are behind a rate
    #: rather than printing it bare.
    repeat: int = 0
    #: Characters in and out. **A proxy for token cost, and named as one**: P11 asks for
    #: tokens, `OllamaClient.generate` returns a string, and threading `eval_count` out of it
    #: would change the actor protocol every backend implements. Characters are comparable
    #: across arms of the same backend, which is the comparison the arm table makes, and they
    #: are not comparable across models. `summary.py` says so where it prints them.
    prompt_chars: int = 0
    reply_chars: int = 0


def _jsonable(value: Any) -> Any:
    """Anything the encoder does not know, in a form it does.

    Sets, so far -- `GauntletResult.skipped` is one, and it reached the log as the last
    action of a run that had already spent a model call and a full toolchain on every
    attempt. The whole record was then discarded by a `TypeError`, which is the worst place
    for this to fail and the reason it is handled generally rather than by converting that
    one field: the next set added to a result would do the same thing again.
    """
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"cannot log a {type(value).__name__}")


def _append_log(attempts: list[Attempt], *, announce: bool = True) -> None:
    """Append these attempts to the log, flushed before returning.

    **Called per target rather than per run, because a run that does not finish used to
    record nothing.** Two interruptions on one afternoon cost 44 attempts between them --
    an out-of-memory kill and a deliberate stop after the endpoint started returning 429 --
    and the second of those threw away the only accepted repair an external bed had ever
    produced, `urlparse` 63 -> 6. The rows existed in memory for forty minutes with nothing
    but the end of the loop standing between them and disk.

    Attempts are independent records; nothing about one depends on the run completing, so
    nothing about writing one should either.
    """
    if not attempts:
        return
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as handle:
        for attempt in attempts:
            row = {"timestamp": time.time(), **asdict(attempt)}
            handle.write(json.dumps(row, default=_jsonable) + "\n")
        handle.flush()
    if announce:
        say(f"\n{DIM}logged {len(attempts)} attempt(s) to {LOG.relative_to(ROOT)}{RESET}")


# ---- P11's measures ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArmResult:
    """What one arm did, over every target it was asked to repair.

    P11 lists the measures: *"violation rate, erosion and verbosity trajectory, convergence
    rate and retry counts, token cost, and functional correctness -- so we can show the gate
    does not break the code."* Four of those are here, and the two that are not say so:

    * **convergence** and **retries** come straight from the log;
    * **functional correctness** is `tests_pass` on the accepted attempt, which is the
      "does not break the code" half and the one a gate can fail on;
    * **cost** is characters, and `cost_note` states what that is and is not;
    * **erosion and verbosity trajectory** need a repository measured over time rather than
      one repair at a time, so they are `oxn volume`'s output across a run rather than this
      log's, and are not computed here.
    """

    arm: str
    backend: str
    targets: int
    #: Samples per target behind the rates below. One is an anecdote about a stochastic
    #: process; the table prints this so a reader cannot mistake which they are looking at.
    repeats: int
    converged: int
    attempts: int
    #: Accepted repairs whose test run passed. Equal to `converged` unless the gate ever
    #: accepts code that fails its tests -- which is the thing worth noticing, so it is
    #: counted separately rather than assumed.
    correct: int
    prompt_chars: int
    reply_chars: int
    seconds: float
    #: Attempts the model was not allowed to finish -- it stopped at `num_predict` with an
    #: answer half written. Counted apart from a failed repair because it is not one, and
    #: because **arms differ in prompt size**: the pilot logged `hybrid` at 646,451 characters
    #: against `none` at 54,543, on one budget. An arm carrying more guidance has less room
    #: to answer, truncates more often, and would score worse for a reason that is about the
    #: budget rather than the guidance. Folding these into "did not converge" would have
    #: biased the table *against* the treatment.
    truncated: int = 0

    @property
    def convergence_rate(self) -> float:
        return self.converged / self.targets if self.targets else 0.0

    @property
    def attempts_per_target(self) -> float:
        return self.attempts / self.targets if self.targets else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "arm": self.arm,
            "backend": self.backend,
            "targets": self.targets,
            "repeats": self.repeats,
            "converged": self.converged,
            "convergence_rate": round(self.convergence_rate, 4),
            "attempts": self.attempts,
            "attempts_per_target": round(self.attempts_per_target, 2),
            "functionally_correct": self.correct,
            "prompt_chars": self.prompt_chars,
            "reply_chars": self.reply_chars,
            "seconds": round(self.seconds, 1),
            "truncated": self.truncated,
        }


#: Below this, the table says so. A judgement rather than a power analysis -- but not an
#: arbitrary one: **n=2 was measured to reverse.** Two runs of one configuration (same
#: target, same arms, same models, same flags) gave `hybrid` 1/2 against `none` 0/2, and
#: then `none` 2/2 against `hybrid` 0/2. A complete reversal of the ordering, from noise
#: alone. Five is where the warning stops, and it is a guess about where noise stops
#: dominating rather than a claim that it does.
ENOUGH_SAMPLES = 5

#: Said wherever a rate is printed over too few samples, because such a rate renders exactly
#: like one that means something.
SMALL_SAMPLE_NOTE = (
    "`generate` is temperature 0 and not deterministic, and two runs of one configuration "
    "here reversed outright -- hybrid 1/2 vs none 0/2, then none 2/2 vs hybrid 0/2, same "
    f"target and same flags. Below n={ENOUGH_SAMPLES} treat an ordering as noise; --repeat "
    "buys samples, and n is a judgement rather than a power analysis."
)

#: Said whenever `cut` is non-zero, because a truncated attempt is not a failed repair and
#: the difference decides what the column above it means.
TRUNCATION_NOTE = (
    "`cut` counts attempts the model was not allowed to finish -- it stopped at "
    "`num_predict` with an answer half written. Those are excluded from `conv`'s numerator "
    "and are not evidence about the arm: arms differ in prompt size, so the one carrying "
    "more guidance has less room to answer and truncates more. Raise OXN_OLLAMA_NUM_PREDICT "
    "or pick a smaller target, then re-run -- do not read an ordering across unequal `cut`."
)

#: Said wherever the cost columns are printed, because a number without it is misread.
COST_NOTE = (
    "cost is characters, not tokens: `generate` returns a string, and threading `eval_count` "
    "out of it would change the protocol every backend implements. Comparable across arms of "
    "one backend -- which is the comparison an arm table makes -- and not across models."
)


def latest_run(rows: list[dict[str, Any]]) -> str:
    """The most recent invocation in the log, or `""` when none is identified.

    What `report` shows by default. Pooling every run ever made is almost never the question
    -- runs differ in bed, in depth, and in whether they were any good -- and the pooled
    answer looks exactly like a single experiment's.
    """
    identified = [str(row["run"]) for row in rows if row.get("run")]
    return max(identified) if identified else ""


def measures(rows: list[dict[str, Any]], run: str | None = None) -> list[ArmResult]:
    """One row per (arm, backend) within one run, worst convergence first.

    `run` selects the invocation; `None` pools everything, which is what the log did
    unconditionally until a grid's control arm inherited a pilot's accepted repair and
    reported 50% convergence on the strength of a run already known to be invalid.

    Attempts logged before the arms existed carry no arm, and are grouped under `""` rather
    than folded into a named one. Silently attributing them to `hybrid` -- which is what the
    harness did then -- would put real numbers from a different experiment into its row.
    """
    if run is not None:
        rows = [row for row in rows if str(row.get("run", "")) == run]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row.get("arm", ""), row.get("backend", "")), []).append(row)
    found = [_result(arm, backend, group) for (arm, backend), group in grouped.items()]
    return sorted(found, key=lambda r: (r.convergence_rate, r.arm))


def _result(arm: str, backend: str, rows: list[dict[str, Any]]) -> ArmResult:
    """One group's measures. Targets are counted by name, since each has several attempts."""
    by_target: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_target.setdefault(row["target"], []).append(row)
    return ArmResult(
        arm=arm or "(unrecorded)",
        backend=backend or "(unrecorded)",
        targets=len(by_target),
        repeats=len({row.get("repeat", 0) for row in rows}),
        converged=_converged(by_target),
        truncated=_truncated(rows),
        # Truncated rows are excluded here for the same reason they are excluded from
        # `converged`: `attempts_per_target` would otherwise carry the confound that column
        # just lost, and an arm with a bigger prompt would read as needing more tries.
        attempts=len(rows) - _truncated(rows),
        correct=_functionally_correct(rows),
        prompt_chars=int(_total(rows, "prompt_chars")),
        reply_chars=int(_total(rows, "reply_chars")),
        seconds=_total(rows, "seconds"),
    )


def _converged(by_target: dict[str, list[dict[str, Any]]]) -> int:
    """Targets where some attempt was accepted. Per target, not per attempt.

    A target repaired on the third try converged once, not three times, and counting
    attempts here would reward an arm for needing more of them.
    """
    return sum(1 for group in by_target.values() if any(row.get("accepted") for row in group))


def _functionally_correct(rows: list[dict[str, Any]]) -> int:
    """Accepted repairs whose test run actually passed.

    Counted rather than assumed equal to `converged`. P11 asks for functional correctness so
    the result can show "the gate does not break the code", and a number derived from
    acceptance could not show it -- it would be the gate grading itself.
    """
    return sum(
        1 for row in rows if row.get("accepted") and row.get("gauntlet", {}).get("tests_pass")
    )


#: What `oxn.llm` says when a model stops at its token budget with an answer half written.
#: Matched on rather than re-derived, so the log stays readable without the client.
TRUNCATION_MARKER = "stopped at the token limit"


def _truncated(rows: list[dict[str, Any]]) -> int:
    """Attempts that ended because the model ran out of room, not because it was wrong.

    Found on the first honest attempt the httpx bed produced: `glm-5.3:cloud` wrote 123,471
    characters, hit `num_predict`, and the extracted candidate was a function body ending
    mid-block. The harness filed it as `tests FAIL types FAIL target_present False` --
    indistinguishable from a repair that was simply bad.
    """
    return sum(
        1 for row in rows if row.get("cut_off") or TRUNCATION_MARKER in str(row.get("error", ""))
    )


def _total(rows: list[dict[str, Any]], key: str) -> float:
    """One numeric column, summed, tolerating rows written before it existed."""
    return sum(float(row.get(key) or 0) for row in rows)


def read_log(path: Path | None = None) -> list[dict[str, Any]]:
    """Every attempt ever logged, or an empty list when nothing has run."""
    found = path or LOG
    if not found.exists():
        return []
    return [json.loads(line) for line in found.read_text().splitlines() if line.strip()]
