#!/usr/bin/env python3
"""Make OXN repair its own violations, and record whether it works.

OXN gates coding agents on complexity. It also violates its own ceilings -- `_walk` scores
52 against a limit of 12 -- and P7 builds the gate itself, whose exit criterion would
otherwise be verified against a codebase failing its own rules. Rather than hand-refactor,
this drives the loop OXN exists to create, on OXN:

    select a violation -> ask a model to repair it -> verify deterministically -> judge

Two models, deliberately different. **glm** writes (the actor); **deepseek** assesses (the
judge). A model grading its own output is not an independent check, and the agreement rate
between judge and gauntlet is itself worth measuring.

**The judge never overrules the gauntlet.** A candidate that fails tests, types, lint or the
shredding detector is rejected before a judge sees it. The judge only chooses among
candidates that already work -- because "the score went down" is exactly the gaming
docs/metrics.md §10.5 predicts an agent will do.

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
import re
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
from gauntlet import (
    SCRATCH,
    GauntletResult,
    Sandbox,
    measure,
    run_gauntlet,
)

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


def select_targets(ceiling: int, limit: int, skip: set[str]) -> list[Target]:
    """Functions whose cognitive complexity exceeds the ceiling, worst first."""
    result = subprocess.run(
        [sys.executable, "-m", "oxn", "metrics", "--json", "--limit", "60", "src/oxn"],
        cwd=ROOT,
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
    seconds: float = 0.0
    #: The extracted candidate and the raw reply it came from. A few KB each, and the only
    #: way to diagnose a bug in extraction after the fact -- which is precisely the analysis
    #: that was impossible when only the verdict was recorded.
    candidate: str = ""
    reply: str = ""
    #: What to tell the actor if there is another attempt. Carried on the attempt rather
    #: than returned separately, so the loop cannot forget to thread it through.
    next_feedback: str = ""


def repair(
    targets: list[Target],
    *,
    ceiling: int,
    retries: int,
    dry_run: bool,
    allow_extraction: bool = False,
    model: str = "",
    judge_model: str = "",
    host: str = "",
) -> list[Attempt]:
    """Attempt to repair each target, verifying every candidate before judging it.

    Models are chosen per run: an explicit flag wins, then the environment, then the
    defaults. Nothing here assumes a particular model -- the loop wants *an* actor and *a*
    judge, and which ones is the operator's business.
    """
    from oxn.llm import OllamaClient
    from oxn.profiles import profile_for_path

    actor: Any = _FakeClient() if dry_run else _client(OllamaClient.from_env(), model, host)
    judge: Any = (
        _FakeClient() if dry_run else _client(OllamaClient.judge_from_env(), judge_model, host)
    )
    if not dry_run:
        say(f"{DIM}actor {actor.model}  judge {judge.model}  at {actor.host}{RESET}")

    DIFFS.mkdir(parents=True, exist_ok=True)
    attempts: list[Attempt] = []

    for target in targets:
        say(f"\n{BOLD}{target.leaf}{RESET} {DIM}{target.path} scores {target.score:.0f}{RESET}")
        before = measure(None, target)
        profile = profile_for_path(target.path)
        if profile is None:
            continue

        sandbox = Sandbox(target.leaf)
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
                ceiling=ceiling,
                allow_extraction=allow_extraction,
            )
            feedback = ""
            for index in range(1, retries + 1):
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

    if dry_run:
        # The fake client emits fixed junk, so these rows would be indistinguishable from
        # real attempts in a benchmark record that exists to be read later.
        say(f"\n{DIM}dry run -- not logged{RESET}")
    else:
        _append_log(attempts)
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
        candidate, reply = ask_actor(
            run.actor,
            Ask(
                target=run.target,
                ceiling=run.ceiling,
                file_source=original_file.decode(errors="replace"),
                feedback=feedback,
                allow_extraction=run.allow_extraction,
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


def _append_log(attempts: list[Attempt]) -> None:
    if not attempts:
        return
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as handle:
        for attempt in attempts:
            handle.write(json.dumps({"timestamp": time.time(), **asdict(attempt)}) + "\n")
    say(f"\n{DIM}logged {len(attempts)} attempt(s) to {LOG.relative_to(ROOT)}{RESET}")


def _client(base: Any, model: str, host: str) -> Any:
    """Apply per-run overrides to a client built from the environment."""
    import dataclasses

    changes: dict[str, Any] = {}
    if model:
        changes["model"] = model
    if host:
        changes["host"] = host
    return dataclasses.replace(base, **changes) if changes else base


class _FakeClient:
    """A deterministic stand-in, so the harness itself has tests that need no model."""

    model = "dry-run"
    host = "(none)"

    def generate(self, prompt: str, *, temperature: float = 0.0, system: str = "") -> str:
        """A reply that defines the function actually asked for.

        A fixed `def placeholder()` stopped exercising anything once the harness began
        rejecting candidates that omit the target: every dry run failed at extraction and
        the gauntlet was never reached. Reading the name back out of the prompt keeps the
        dry run a test of the plumbing rather than of the check that guards it.
        """
        del temperature, system
        match = re.search(r"replacement for `([^`]+)`", prompt)
        name = match.group(1) if match else "placeholder"
        return f"def {name}(*args, **kwargs):\n    return None\n"

    def generate_json(self, prompt: str, *, system: str = "") -> dict[str, object]:
        del prompt, system
        return {"verdict": "reject", "concerns": ["dry run"], "genuinely_simpler": False}


# ---- reporting ------------------------------------------------------------------------------


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
        scored = [row for row in group if row["gauntlet"]]
        # A score only counts as a result if the code it was measured on actually works.
        # Attempts that fail their tests, or that deleted the target outright, still produce
        # a number -- and it is usually a flatteringly low one.
        valid = [
            row
            for row in scored
            if row["gauntlet"].get("tests_pass") and row["gauntlet"].get("target_present", True)
        ]
        best = min((row["gauntlet"]["score_after"] for row in valid), default=None)
        # The first attempt may have died before measuring anything, so take the first row
        # that has a measurement rather than the first row.
        first = scored[0]["gauntlet"].get("score_before") if scored else None
        status = (
            f"{GREEN}accepted{RESET}" if any(r["accepted"] for r in group) else f"{RED}no{RESET}"
        )
        errors = sum(1 for row in group if row.get("error"))
        note = f"  ({errors} failed to run)" if errors else ""
        say(f"  {name:32} {_num(first)} -> {_num(best)}  attempts={len(group)}  {status}{note}")

    judged = [r for r in rows if r.get("judge", {}).get("verdict") in {"accept", "reject"}]
    if judged:
        agree = sum(
            1
            for r in judged
            if (r["judge"]["verdict"] == "accept") == bool(r["gauntlet"].get("tests_pass"))
        )
        say(f"\n  judge/gauntlet agreement: {agree}/{len(judged)}")


def _num(value: float | None) -> str:
    return "--" if value is None else f"{value:g}"


def plan(ceiling: int, limit: int) -> None:
    for target in select_targets(ceiling, limit, skip=set()):
        say(f"  {target.score:5.0f}  {target.leaf:32} {DIM}{target.path}{RESET}")
        for line in target.trail[:3]:
            say(f"{DIM}          {line}{RESET}")


def main(argv: list[str] | None = None) -> int:
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
        "--model", default="", help="actor model (default: $OXN_OLLAMA_MODEL, else glm-5.3:cloud)"
    )
    parser.add_argument(
        "--judge-model",
        default="",
        help="judge model (default: $OXN_OLLAMA_JUDGE_MODEL, else deepseek-v4-pro:cloud)",
    )
    parser.add_argument("--host", default="", help="Ollama host (default: $OXN_OLLAMA_HOST)")
    parser.add_argument("--skip", action="append", default=[], help="qualified name to skip")
    args = parser.parse_args(argv)

    if args.command == "plan":
        plan(args.ceiling, args.limit)
        return 0
    if args.command == "report":
        summarise()
        return 0

    targets = select_targets(args.ceiling, args.limit, skip=set(args.skip))
    if not targets:
        say("nothing over the ceiling")
        return 0
    attempts = repair(
        targets,
        ceiling=args.ceiling,
        allow_extraction=args.allow_extraction,
        retries=args.retries,
        dry_run=args.dry_run,
        model=args.model,
        judge_model=args.judge_model,
        host=args.host,
    )
    return 0 if any(a.accepted for a in attempts) or args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
