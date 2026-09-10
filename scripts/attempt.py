"""One repair attempt: ask the model, splice the answer, and let the gauntlet judge it.

Split out of `dogfood.py` when it crossed its own `file_sloc` ceiling for the third time --
`runlog.py` and `targets.py` were the first two. The seam is the same one `oxn check` keeps
finding here: `dogfood.py` runs a *campaign* -- pick targets, build a sandbox, loop over
arms and repeats -- and this file is what one iteration of that loop consists of. Nothing
here knows about beds, sessions or the command line.

The order is the harness's one real invariant and lives in `_judge_if_it_earned_one`: a
model is asked for an opinion only about code that already passes the deterministic checks.
The judge can reject a candidate and never rescue one.
"""

from __future__ import annotations

import difflib
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from actor import Ask, _defines, _feedback, ask_actor, ask_judge
from console import DIM, GREEN, RED, RESET, YELLOW, say
from gauntlet import SCRATCH, GauntletResult, Sandbox, run_gauntlet
from runlog import Attempt
from targets import Target, function_span

DIFFS = SCRATCH / "diffs"

if TYPE_CHECKING:  # pragma: no cover
    from arms import Arm


@dataclass(frozen=True, slots=True)
class Run:
    """Everything one target's repair loop needs, fixed for the whole loop.

    A record because these do not vary between attempts, and passing eight arguments down
    per attempt would put the signature over the very ceiling this harness enforces.
    """

    actor: Any
    judge: Any
    sandbox: Sandbox
    target: Target
    profile: Any
    before: Any
    ceiling: int
    allow_extraction: bool
    #: The channels this run opens, and therefore whether a retry means anything.
    arm: Arm
    #: What answered. Recorded per attempt so a log can be split by actor as well as by arm.
    backend: str
    #: The bed's own checks. Carried rather than looked up, so the loop cannot verify one
    #: bed's repair with another's toolchain.
    checks: tuple[tuple[str, ...], ...] = ()
    #: Which sample of this (arm, target) this run is. See `runlog.Attempt.repeat`.
    repeat: int = 0
    #: The invocation every arm of one grid shares. See `runlog.Attempt.run`.
    run_id: str = ""


def one_attempt(run: Run, index: int, feedback: str) -> Attempt | None:
    """One ask, one verification, one verdict. `None` when the target cannot be located."""
    attempt = _Try(run=run, index=index, started=time.perf_counter())
    path = run.sandbox.path / run.target.path
    original_file = path.read_bytes()
    span = function_span(path, run.target.leaf, run.profile)
    if span is None:
        return None
    original = original_file[span[0] : span[1]].decode()

    asked = _ask_for_a_candidate(run, attempt, feedback, original_file)
    if isinstance(asked, Attempt):
        return asked
    candidate, reply, prompt = asked

    if not _defines(candidate, run.target.leaf):
        # Splicing this would delete the function. Reject now rather than after a full test,
        # lint and type run -- five minutes to learn what the reply already showed.
        say(f"{RED}  attempt {index}: reply did not define {run.target.leaf}{RESET}")
        return _failed(
            attempt,
            error=f"the reply did not define {run.target.leaf}",
            next_feedback=f"- Your reply did not contain a `def {run.target.leaf}(...)`.",
            candidate=candidate,
            reply=reply,
        )

    path.write_bytes(original_file[: span[0]] + candidate.encode() + original_file[span[1] :])
    gauntlet = run_gauntlet(run.sandbox, run.target, run.before, run.ceiling, run.checks)
    verdict = _judge_if_it_earned_one(run, gauntlet, original, candidate)
    accepted = gauntlet.passed and verdict.get("verdict") == "accept"
    _report_attempt(index, gauntlet, verdict, accepted)

    if accepted:
        _write_diff(run.target, original, candidate)
    else:
        # Restore, so every attempt starts from the original rather than the last failure.
        path.write_bytes(original_file)

    return Attempt(
        run.target.leaf,
        run.target.path,
        index,
        accepted,
        asdict(gauntlet),
        verdict,
        seconds=attempt.elapsed,
        candidate=candidate,
        reply=reply,
        next_feedback=_feedback(gauntlet),
        arm=run.arm.name,
        backend=run.backend,
        run=run.run_id,
        repeat=run.repeat,
        prompt_chars=len(prompt),
        reply_chars=len(reply),
    )


@dataclass(frozen=True, slots=True)
class _Try:
    """One attempt in progress: which run, which number, and when it started."""

    run: Run
    index: int
    started: float

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.started


def _failed(
    attempt: _Try, *, error: str, next_feedback: str, last: bool = False, **extra: str
) -> Attempt:
    """An attempt that never reached the gauntlet. Logged like any other: it is data.

    `last` marks the one failure another attempt cannot improve on -- a reply cut off at the
    token budget. It rides on the attempt rather than being returned beside it so the loop
    cannot forget to check it, which is the same reason `next_feedback` is here.
    """
    return Attempt(
        attempt.run.target.leaf,
        attempt.run.target.path,
        attempt.index,
        False,
        {},
        error=error,
        seconds=attempt.elapsed,
        next_feedback=next_feedback,
        arm=attempt.run.arm.name,
        backend=attempt.run.backend,
        run=attempt.run.run_id,
        repeat=attempt.run.repeat,
        cut_off=last,
        **extra,
    )


def _judge_if_it_earned_one(
    run: Run, gauntlet: GauntletResult, original: str, candidate: str
) -> dict[str, Any]:
    """A model is asked for an opinion only about code that already works.

    The judge cannot rescue a candidate, only reject one, and that ordering is the harness's
    central claim -- so it is expressed as control flow rather than as a convention.
    """
    if not gauntlet.passed:
        return {}
    try:
        return ask_judge(
            run.judge, original, candidate, gauntlet.score_before, gauntlet.score_after
        )
    except Exception as error:  # noqa: BLE001 - an unavailable judge is a data point too
        return {"verdict": "unavailable", "error": str(error)[:200]}


def _mark(label: str, gauntlet: GauntletResult) -> str:
    """One check's verdict, and `skip` for one the bed never declared.

    `GauntletResult.skipped` has kept "not applicable" apart from "failed" since it was
    added, and this line printed `FAIL` for both -- so every Python-only bed read
    `lint FAIL types FAIL` on every attempt, for two commands that were never run. The same
    thing `_run_toolchain`'s docstring calls worse than a red row, in the one place a human
    actually watches.
    """
    if label in gauntlet.skipped:
        return f"{label} skip"
    return f"{label} {'ok' if getattr(gauntlet, f'{label}_pass') else 'FAIL'}"


def _ask_for_a_candidate(
    run: Run, attempt: _Try, feedback: str, source: bytes
) -> tuple[str, str, str] | Attempt:
    """The model's answer, or the attempt that records why there is not one.

    Its own unit because it has its own contract: everything from here on works on a
    candidate, and this is the only place that decides whether there is one. `_one_attempt`
    went over `function_sloc` when truncation gained a branch, which is the gate saying that
    "ask" and "verify" had been sharing a function.
    """
    try:
        return ask_actor(
            run.actor,
            Ask(
                target=run.target,
                ceiling=run.ceiling,
                file_source=source.decode(errors="replace"),
                feedback=feedback,
                allow_extraction=run.allow_extraction,
                arm=run.arm,
            ),
        )
    except Exception as error:  # noqa: BLE001 - a model failure is a data point, not a crash
        say(f"{RED}  attempt {attempt.index}: actor failed: {error}{RESET}")
        # A retry exists so the model can act on feedback about a bad repair. A cut-off
        # reply carries no such signal -- the prompt is unchanged, the temperature is 0, and
        # the model scales its deliberation to whatever room it has -- so retrying buys the
        # same truncation more slowly. `last=True` ends this (target, repeat) at one row.
        return _failed(
            attempt,
            error=str(error)[:200],
            next_feedback=feedback,
            last=_was_cut_off(error),
        )


def _was_cut_off(error: BaseException) -> bool:
    """Is this the one model failure that another attempt cannot improve on?

    Asked by type rather than by message: `oxn.llm.ReplyCutOff` exists so a harness does not
    have to parse prose to tell "the host was busy" from "the answer was cut in half".
    """
    from oxn.llm import ReplyCutOff

    return isinstance(error, ReplyCutOff)


def _report_attempt(
    index: int, gauntlet: GauntletResult, verdict: dict[str, Any], accepted: bool
) -> None:
    marks = [
        *(_mark(label, gauntlet) for label in ("tests", "lint", "types")),
        f"score {gauntlet.score_before:.0f}->{gauntlet.score_after:.0f}",
    ]
    if gauntlet.shredded:
        marks.append(f"{YELLOW}SHREDDED{RESET}")
    colour = GREEN if accepted else RED
    decision = verdict.get("verdict", "-")
    say(
        f"  attempt {index}: " + "  ".join(marks) + f"  judge={decision}  {colour}"
        f"{'ACCEPTED' if accepted else 'rejected'}{RESET}"
    )
    for concern in verdict.get("concerns", [])[:3]:
        say(f"{DIM}      concern: {concern}{RESET}")


def _write_diff(target: Target, original: str, candidate: str) -> None:

    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        candidate.splitlines(keepends=True),
        fromfile=f"a/{target.path}",
        tofile=f"b/{target.path}",
    )
    out = DIFFS / f"{target.leaf}.diff"
    out.write_text("".join(diff))
    say(f"{GREEN}  diff written: {out}{RESET}")
