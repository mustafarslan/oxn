#!/usr/bin/env python3
"""Make OXN repair its own violations, and record whether it works.

OXN gates coding agents on complexity, and for most of its life it violated its own
ceilings -- `_walk` scored 52 against a limit of 12 -- while P7 builds the gate itself,
whose exit criterion would otherwise be verified against a codebase failing its own rules.
Rather than hand-refactor, this drove the loop OXN exists to create, on OXN:

    select a violation -> ask a model to repair it -> verify deterministically -> judge

Two models, deliberately different. **glm** writes (the actor); **deepseek** assesses (the
judge). A model grading its own output is not an independent check, and the agreement rate
between judge and gauntlet is itself worth measuring.

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
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from actor import (
    Ask,
    _defines,
    _feedback,
    ask_actor,
    ask_judge,
)
from arms import Arm, arm
from beds import SELF, Bed, bed, bed_names
from gauntlet import (
    SCRATCH,
    GauntletResult,
    Sandbox,
    measure,
    run_gauntlet,
)
from runlog import Attempt, _append_log
from summary import summarise

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "benchmarks" / "dogfood-log.jsonl"
DIFFS = SCRATCH / "diffs"


def say(message: str = "") -> None:
    """Print, flushing immediately.

    A repair run spends minutes inside a single model call, and output buffered through a
    pipe shows nothing until the process exits -- which is indistinguishable from a hang.
    """
    print(message, flush=True)


DIM, GREEN, RED, YELLOW, BOLD, RESET = (
    "\033[2m",
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[1m",
    "\033[0m",
)


# ---- targets ------------------------------------------------------------------------------


@dataclass
class Target:
    """A function over the ceiling."""

    qualified_name: str
    path: str
    score: float
    trail: list[str] = field(default_factory=list)

    @property
    def leaf(self) -> str:
        return self.qualified_name.rsplit(".", 1)[-1]


def select_targets(ceiling: int, limit: int, skip: set[str], where: Bed = SELF) -> list[Target]:
    """Functions whose cognitive complexity exceeds the ceiling, worst first.

    `where` used to be `src/oxn`, written into this function. A result measured only on the
    repository the tool was written for is a statement about that repository, which is why
    P11 names three beds and why this now takes one.
    """
    result = subprocess.run(
        [sys.executable, "-m", "oxn", "metrics", "--json", "--limit", "60", *where.sources],
        cwd=where.root,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    targets: list[Target] = []
    for row in payload.get("entities", []):
        if row["value"] <= ceiling or row["qualified_name"] in skip:
            continue
        targets.append(
            Target(qualified_name=row["qualified_name"], path=row["path"], score=row["value"])
        )
        if len(targets) >= limit:
            break

    for target in targets:
        target.trail = _trail_for(target)
    return targets


def _trail_for(target: Target) -> list[str]:
    """The increment trail: the explanation an agent is supposed to act on."""
    from oxn.languages import get_parser
    from oxn.metrics import cognitive_complexity
    from oxn.profiles import profile_for_path

    profile = profile_for_path(target.path)
    if profile is None:
        return []
    source = (ROOT / target.path).read_bytes()
    tree = get_parser(profile.name).parse(source)
    for node, name in _iter_functions(tree.root_node, profile):
        if name == target.leaf:
            return cognitive_complexity(node, profile, function_name=name).explain()
    return []


def _iter_functions(root: Any, profile: Any) -> list[tuple[Any, str]]:
    found: list[tuple[Any, str]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        definition = profile.unwrap(node)
        if definition.type in profile.function_like:
            name = profile.entity_name(definition)
            if name:
                found.append((definition, name))
    return found


def function_span(path: Path, leaf: str, profile: Any) -> tuple[int, int] | None:
    """Byte range of a function *including* its decorators, for mechanical replacement.

    Splicing by byte range rather than applying a model-produced diff: LLM diffs mis-apply,
    and the graph builder already records the exact span.
    """
    from oxn.graph.builder import build_file
    from oxn.languages import get_parser

    source = path.read_bytes()
    tree = get_parser(profile.name).parse(source)
    parsed = build_file(path.name, source, profile, tree.root_node)
    for entity in parsed.entities:
        if entity.name == leaf and entity.kind.value in {"function", "method"}:
            return entity.start_byte, entity.end_byte
    return None


# ---- sandbox ------------------------------------------------------------------------------


# ---- the loop -----------------------------------------------------------------------------


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
    #: Which of OXN's three channels the actor is given. `scripts.arms` holds the table;
    #: `hybrid` is what the harness did before the arms existed.
    arm: str = "hybrid"
    model: str = ""
    judge_model: str = ""
    host: str = ""


def repair(targets: list[Target], session: Session, where: Bed = SELF) -> list[Attempt]:
    """Attempt to repair each target, verifying every candidate before judging it."""
    actor, judge = _clients(session)
    if not session.dry_run:
        say(f"{DIM}actor {actor.model}  judge {judge.model}  at {actor.host}{RESET}")

    DIFFS.mkdir(parents=True, exist_ok=True)
    attempts: list[Attempt] = []
    for target in targets:
        say(f"\n{BOLD}{target.leaf}{RESET} {DIM}{target.path} scores {target.score:.0f}{RESET}")
        attempts.extend(_repair_one(target, session, actor, judge, where))

    if session.dry_run:
        # The fake client emits fixed junk, so these rows would be indistinguishable from
        # real attempts in a benchmark record that exists to be read later.
        say(f"\n{DIM}dry run -- not logged{RESET}")
    else:
        _append_log(attempts)
    return attempts


def _clients(session: Session) -> tuple[Any, Any]:
    """The actor and the judge, chosen by name rather than assumed.

    `--dry-run` is the `dry-run` backend rather than a branch here, so the fake goes through
    exactly the path a real backend does: a stand-in reached by a different route tests a
    different thing.
    """
    from actors import build, build_judge

    backend = "dry-run" if session.dry_run else session.backend
    return (
        build(backend, session.model, session.host),
        build_judge(backend, session.judge_model, session.host),
    )


def _repair_one(
    target: Target, session: Session, actor: Any, judge: Any, where: Bed = SELF
) -> list[Attempt]:
    """Every attempt at one target, stopping at acceptance or at the retry budget.

    The sandbox is destroyed however this exits: a run that leaves 100 MB of throwaway
    copies behind after an interrupt is one nobody runs twice.
    """
    from oxn.profiles import profile_for_path

    profile = profile_for_path(target.path)
    if profile is None:
        return []

    before = measure(None, target)
    sandbox = Sandbox(target.leaf, root=where.root)
    attempts: list[Attempt] = []
    try:
        say(f"{DIM}  creating sandbox...{RESET}")
        sandbox.create()
        run = Run(
            actor=actor,
            judge=judge,
            sandbox=sandbox,
            target=target,
            profile=profile,
            before=before,
            ceiling=session.ceiling,
            allow_extraction=session.allow_extraction,
            arm=arm(session.arm),
            backend="dry-run" if session.dry_run else session.backend,
        )
        feedback = ""
        for index in range(1, _attempts_for(session) + 1):
            attempt = _one_attempt(run, index, feedback)
            if attempt is None:
                say(f"{RED}  cannot locate {target.leaf}{RESET}")
                break
            attempts.append(attempt)
            feedback = attempt.next_feedback
            if attempt.accepted:
                break
    finally:
        sandbox.destroy()
    return attempts


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


def _one_attempt(run: Run, index: int, feedback: str) -> Attempt | None:
    """One ask, one verification, one verdict. `None` when the target cannot be located."""
    attempt = _Try(run=run, index=index, started=time.perf_counter())
    path = run.sandbox.path / run.target.path
    original_file = path.read_bytes()
    span = function_span(path, run.target.leaf, run.profile)
    if span is None:
        return None
    original = original_file[span[0] : span[1]].decode()

    try:
        candidate, reply, prompt = ask_actor(
            run.actor,
            Ask(
                target=run.target,
                ceiling=run.ceiling,
                file_source=original_file.decode(errors="replace"),
                feedback=feedback,
                allow_extraction=run.allow_extraction,
                arm=run.arm,
            ),
        )
    except Exception as error:  # noqa: BLE001 - a model failure is a data point, not a crash
        say(f"{RED}  attempt {index}: actor failed: {error}{RESET}")
        return _failed(attempt, error=str(error)[:200], next_feedback=feedback)

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
    gauntlet = run_gauntlet(run.sandbox, run.target, run.before, run.ceiling)
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


def _failed(attempt: _Try, *, error: str, next_feedback: str, **extra: str) -> Attempt:
    """An attempt that never reached the gauntlet. Logged like any other: it is data."""
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


def _report_attempt(
    index: int, gauntlet: GauntletResult, verdict: dict[str, Any], accepted: bool
) -> None:
    marks = [
        f"tests {'ok' if gauntlet.tests_pass else 'FAIL'}",
        f"lint {'ok' if gauntlet.lint_pass else 'FAIL'}",
        f"types {'ok' if gauntlet.types_pass else 'FAIL'}",
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
    import difflib

    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        candidate.splitlines(keepends=True),
        fromfile=f"a/{target.path}",
        tofile=f"b/{target.path}",
    )
    out = DIFFS / f"{target.leaf}.diff"
    out.write_text("".join(diff))
    say(f"{GREEN}  diff written: {out}{RESET}")


# ---- reporting ------------------------------------------------------------------------------


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


def plan(ceiling: int, limit: int) -> None:
    for target in select_targets(ceiling, limit, skip=set()):
        say(f"  {target.score:5.0f}  {target.leaf:32} {DIM}{target.path}{RESET}")
        for line in target.trail[:3]:
            say(f"{DIM}          {line}{RESET}")


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
        "--model", default="", help="actor model (default: $OXN_OLLAMA_MODEL, else glm-5.3:cloud)"
    )
    parser.add_argument(
        "--judge-model",
        default="",
        help="judge model (default: $OXN_OLLAMA_JUDGE_MODEL, else deepseek-v4-pro:cloud)",
    )
    parser.add_argument("--host", default="", help="Ollama host (default: $OXN_OLLAMA_HOST)")
    parser.add_argument("--skip", action="append", default=[], help="qualified name to skip")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.command == "plan":
        plan(args.ceiling, args.limit)
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
            model=args.model,
            judge_model=args.judge_model,
            host=args.host,
        ),
        where,
    )
    return 0 if any(a.accepted for a in attempts) or args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
