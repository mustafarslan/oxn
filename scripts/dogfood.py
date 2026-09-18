#!/usr/bin/env python3
"""Make OXN repair its own violations, and record whether it works.

OXN gates coding agents on complexity, and for most of its life it violated its own
ceilings -- `_walk` scored 52 against a limit of 12 -- while P7 builds the gate itself,
whose exit criterion would otherwise be verified against a codebase failing its own rules.
Rather than hand-refactor, this drove the loop OXN exists to create, on OXN:

    select a violation -> ask a model to repair it -> verify deterministically -> judge

Two models, deliberately different. **kimi** writes (the actor); **deepseek** assesses (the
judge). A model grading its own output is not an independent check, and the agreement rate
between judge and gauntlet is itself worth measuring.

This paragraph said *glm* writes until 2026-09-18, describing the design rather than the
measurement that replaced it on 2026-09-11: `glm-5.3:cloud` fills whatever output budget it
is given and is cut off mid-answer every time -- 6 targets, 6 failures, 120,000-134,000
characters each on the httpx bed. `oxn.llm.DEFAULT_MODEL` had already moved to `kimi-k3:cloud`
and this text had not, which is worse than no text: it is the file's own docstring arguing
against its own default, and following it costs a run to find out.

**The judge never overrules the gauntlet.** A candidate that fails tests, types, lint or the
shredding detector is rejected before a judge sees it. The judge only chooses among
candidates that already work -- because "the score went down" is exactly the gaming
docs/metrics.md §10.5 predicts an agent will do.

**As of 2026-08-31 there is nothing left to repair**: no function in `src` exceeds any
ceiling and the baseline is empty, so `plan` returns nothing and `repair` says so. That is
the outcome this harness existed to reach, and it leaves the harness with a second job --
P10 needs roughly fifty labelled extractions to fit `TRIVIAL_HELPER`, which now have to
come from a lowered `--ceiling` or from other repositories rather than from OXN's own debt.

Nothing here touches the working tree. Every attempt runs in a throwaway copy, and the loop
emits diffs and a JSONL log for a human to review and commit.

    python scripts/dogfood.py plan                 # what is over the ceiling
    python scripts/dogfood.py repair --limit 3     # attempt repairs, write diffs
    python scripts/dogfood.py repair --dry-run     # exercise the plumbing, no model calls
    python scripts/dogfood.py report               # summarise the log

Models are configurable: `--model`, `--judge-model` and `--host` override
`OXN_OLLAMA_MODEL`, `OXN_OLLAMA_JUDGE_MODEL` and `OXN_OLLAMA_HOST`, which in turn override
the defaults. The loop needs *an* actor and *a* judge; which ones is the operator's choice.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arms import arm
from attempt import Run, one_attempt
from beds import SELF, Bed, bed, bed_names
from console import BOLD, DIM, RED, RESET, say
from gauntlet import SCRATCH, Sandbox, measure, verify_bed
from runlog import Attempt, _append_log
from summary import summarise
from targets import Target, plan, select_targets

from oxn.llm import DEFAULT_JUDGE_MODEL, DEFAULT_MODEL

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "benchmarks" / "dogfood-log.jsonl"
DIFFS = SCRATCH / "diffs"


# ---- targets ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Session:
    """How one repair run is configured: which models, and what the loop may do.

    Models are chosen per run -- an explicit flag wins, then the environment, then the
    defaults. Nothing here assumes a particular model *or a particular backend*: the loop
    wants *an* actor and *a* judge, and which ones is the operator's business. That claim
    was in this docstring while `_clients` named `OllamaClient` outright; `scripts.actors`
    is what makes it true.
    """

    ceiling: int
    retries: int
    dry_run: bool = False
    allow_extraction: bool = False
    #: Which actor drives the loop. P11's arms are not all local models -- the one that
    #: decides what the result means is Claude Code under OXN's own hooks -- so this is a
    #: name resolved by `scripts.actors` rather than a fixed client.
    backend: str = "ollama"
    #: How many times to attempt each target. One sample of a stochastic process is an
    #: anecdote; the arm table prints this so a reader can tell which they have.
    repeats: int = 1
    #: Which invocation this is. Shared by every arm of one grid, so a table can be scoped
    #: to the experiment that produced it rather than to everything ever logged.
    run_id: str = ""

    @property
    def is_fake(self) -> bool:
        """True when nothing real answered, however that was asked for.

        `--dry-run` and `--backend dry-run` reach the same `FakeClient`, and only the first
        used to suppress logging -- so the second wrote fabricated attempts into
        `benchmarks/dogfood-log.jsonl` beside real ones, where nothing downstream could tell
        them apart. A record that mixes invented rows with measured ones is worse than an
        empty one.
        """
        return self.dry_run or self.backend == "dry-run"

    #: Which of OXN's three channels the actor is given. `scripts.arms` holds the table;
    #: `hybrid` is what the harness did before the arms existed.
    arm: str = "hybrid"
    model: str = ""
    judge_model: str = ""
    host: str = ""


def repair(targets: list[Target], session: Session, where: Bed = SELF) -> list[Attempt]:
    """Attempt to repair each target, verifying every candidate before judging it."""
    crew = Crew(*_clients(session))
    if not session.is_fake:
        say(f"{DIM}actor {crew.actor.model}  judge {crew.judge.model}  at {crew.actor.host}{RESET}")

    DIFFS.mkdir(parents=True, exist_ok=True)
    attempts: list[Attempt] = []
    for sample in range(max(1, session.repeats)):
        for index, target in enumerate(targets):
            made = _one_target(target, session, crew, where, sample)
            attempts.extend(made)
            if any(attempt.endpoint_unavailable for attempt in made):
                remaining = len(targets) - index - 1
                say(
                    f"\n{RED}the model endpoint refused the request; stopping rather than "
                    f"asking it {remaining} more times{RESET}"
                )
                return _finish(attempts, session)

    return _finish(attempts, session)


def _one_target(
    target: Target, session: Session, crew: Crew, where: Bed, sample: int
) -> list[Attempt]:
    """One target announced, repaired, and written to the log before the next one starts.

    The write is here rather than after the loop because a run that does not finish used to
    record nothing: two interruptions in one afternoon cost 44 attempts, including the only
    accepted repair an external bed had produced. Announcing, repairing and recording are one
    unit of work per target, and splitting the recording out of it is what made them
    separable enough to lose.
    """
    of = f" {DIM}[{sample + 1}/{session.repeats}]{RESET}" if session.repeats > 1 else ""
    say(f"\n{BOLD}{target.leaf}{RESET} {DIM}{target.path} scores {target.score:.0f}{RESET}{of}")
    made = _repair_one(target, session, crew, where, sample)
    if not session.is_fake:
        _append_log(made, announce=False)
    return made


def _finish(attempts: list[Attempt], session: Session) -> list[Attempt]:
    """Say what was written. The writing itself already happened, per target."""
    if session.is_fake:
        # The fake client emits fixed junk, so these rows would be indistinguishable from
        # real attempts in a benchmark record that exists to be read later.
        say(f"\n{DIM}dry run -- not logged{RESET}")
    elif attempts:
        say(f"\n{DIM}logged {len(attempts)} attempt(s) to benchmarks/dogfood-log.jsonl{RESET}")
    return attempts


@dataclass(frozen=True, slots=True)
class Crew:
    """Who answers, and who grades. They are built together and used together.

    A record because they were two positional arguments threaded through every layer, and
    the parameter ceiling objected the moment a sixth was needed -- correctly: `_repair_one`
    was taking a target plus five facts about the run, and only one of them was the target.
    """

    actor: Any
    judge: Any


def _clients(session: Session) -> tuple[Any, Any]:
    """The actor and the judge, chosen by name rather than assumed.

    `--dry-run` is the `dry-run` backend rather than a branch here, so the fake goes through
    exactly the path a real backend does: a stand-in reached by a different route tests a
    different thing.
    """
    from actors import build, build_judge

    backend = "dry-run" if session.is_fake else session.backend
    return (
        build(backend, session.model, session.host),
        build_judge(backend, session.judge_model, session.host),
    )


def _repair_one(
    target: Target, session: Session, crew: Crew, where: Bed = SELF, sample: int = 0
) -> list[Attempt]:
    """Every attempt at one target, stopping at acceptance or at the retry budget.

    The sandbox is destroyed however this exits: a run that leaves 100 MB of throwaway
    copies behind after an interrupt is one nobody runs twice.
    """
    from oxn.profiles import profile_for_path

    profile = profile_for_path(target.path)
    if profile is None:
        return []

    before = measure(None, target, root=where.root)
    sandbox = Sandbox(target.leaf, root=where.root, venv=where.venv, prepare=where.prepare)
    attempts: list[Attempt] = []
    try:
        say(f"{DIM}  creating sandbox...{RESET}")
        sandbox.create()
        # The bed's own checks, on the bed's own untouched code. A tree that fails them
        # cannot say anything about a repair, and it fails them the same way a bad repair
        # does -- which is how an empty virtualenv produced `tests FAIL` on every attempt of
        # every arm and would have rendered as a table.
        say(f"{DIM}  verifying the bed...{RESET}")
        verify_bed(sandbox, where.verify)
        run = Run(
            actor=crew.actor,
            judge=crew.judge,
            sandbox=sandbox,
            target=target,
            profile=profile,
            before=before,
            ceiling=session.ceiling,
            allow_extraction=session.allow_extraction,
            arm=arm(session.arm),
            backend="dry-run" if session.is_fake else session.backend,
            checks=where.verify,
            repeat=sample,
            run_id=session.run_id,
        )
        feedback = ""
        for index in range(1, _attempts_for(session) + 1):
            attempt = one_attempt(run, index, feedback)
            if attempt is None:
                say(f"{RED}  cannot locate {target.leaf}{RESET}")
                break
            attempts.append(attempt)
            feedback = attempt.next_feedback
            if attempt.accepted or attempt.cut_off:
                break
    finally:
        sandbox.destroy()
    return attempts


# ---- reporting ------------------------------------------------------------------------------


def new_run_id() -> str:
    """An identifier for one invocation, sortable so `latest` means what it says.

    A timestamp rather than a random token, because the useful default is "the run I just
    did" and comparing ids has to answer that without a separate index.
    """
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime())


def _attempts_for(session: Session) -> int:
    """How many times this arm may answer.

    An arm with no enforcement channel gets one attempt. There is nothing to retry *against*
    without the gate's rejection, so extra rounds would be re-rolls of the dice, and a run
    that scored them would credit advice with what was really just more samples.
    """
    return session.retries if arm(session.arm).enforcement else 1


def _backend_names() -> tuple[str, ...]:
    """The registered backends, so `--help` lists what exists rather than what was typed."""
    from actors import backend_names

    return backend_names()


def _arm_names() -> tuple[str, ...]:
    """The registered arms, so `--help` lists the experiment rather than a guess."""
    from arms import arm_names

    return arm_names()


def _parser() -> argparse.ArgumentParser:
    """The command line, which is the experiment's dial panel.

    Its own function because it grew into one: `--arm` and `--backend` are the two axes P11
    varies, and every arm and backend added widens this and nothing else. `main` dispatches.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["plan", "repair", "report"])
    parser.add_argument(
        "--allow-extraction",
        action="store_true",
        help="let the actor extract helpers; the shredding gate still polices them",
    )
    parser.add_argument("--ceiling", type=int, default=12)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="samples per target. One is an anecdote; the arm table prints which you have",
    )
    parser.add_argument("--dry-run", action="store_true", help="exercise the loop with no model")
    parser.add_argument(
        "--bed",
        default="self",
        choices=bed_names(),
        help="where targets come from. Beds other than `self` need fetching first",
    )
    parser.add_argument(
        "--arm",
        default="hybrid",
        choices=_arm_names(),
        help="which of OXN's channels the actor gets. `none` is the control",
    )
    parser.add_argument(
        "--backend",
        default="ollama",
        choices=_backend_names(),
        help="what drives the loop. `claude-code` is the arm P11 says the result depends on",
    )
    parser.add_argument(
        "--model",
        default="",
        help=f"actor model (default: $OXN_OLLAMA_MODEL, else {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--judge-model",
        default="",
        help=f"judge model (default: $OXN_OLLAMA_JUDGE_MODEL, else {DEFAULT_JUDGE_MODEL})",
    )
    parser.add_argument("--host", default="", help="Ollama host (default: $OXN_OLLAMA_HOST)")
    parser.add_argument("--skip", action="append", default=[], help="qualified name to skip")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.command == "plan":
        plan(args.ceiling, args.limit, bed(args.bed))
        return 0
    if args.command == "report":
        summarise()
        return 0

    where = bed(args.bed)
    targets = select_targets(args.ceiling, args.limit, skip=set(args.skip), where=where)
    if not targets:
        say("nothing over the ceiling")
        return 0
    attempts = repair(
        targets,
        Session(
            ceiling=args.ceiling,
            retries=args.retries,
            dry_run=args.dry_run,
            allow_extraction=args.allow_extraction,
            backend=args.backend,
            arm=args.arm,
            repeats=args.repeat,
            run_id=new_run_id(),
            model=args.model,
            judge_model=args.judge_model,
            host=args.host,
        ),
        where,
    )
    return 0 if any(a.accepted for a in attempts) or args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
