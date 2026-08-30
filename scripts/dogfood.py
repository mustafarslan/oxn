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
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(os.environ.get("OXN_SCRATCH", "/tmp")) / "oxn-dogfood"
LOG = ROOT / "benchmarks" / "dogfood-log.jsonl"
DIFFS = SCRATCH / "diffs"

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


class Sandbox:
    """A throwaway copy of the repository. The working tree is never touched."""

    def __init__(self, name: str) -> None:
        self.path = SCRATCH / name
        self.python = self.path / ".venv" / "bin" / "python"

    def create(self) -> None:
        if self.path.exists():
            shutil.rmtree(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            ROOT,
            self.path,
            ignore=shutil.ignore_patterns(
                ".git",
                ".venv",
                ".venvs",
                ".oxn",
                "benchmarks/corpora",
                "tools",
                "__pycache__",
                "*.pyc",
                ".mypy_cache",
                ".ruff_cache",
                ".pytest_cache",
            ),
        )
        subprocess.run(["uv", "venv", "-q", str(self.path / ".venv")], check=True)
        subprocess.run(
            ["uv", "pip", "install", "--python", str(self.python), "-q", "-e", f"{self.path}[dev]"],
            check=True,
        )

    def run(self, *args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.python), *args],
            cwd=self.path,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def destroy(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


# ---- the gauntlet -------------------------------------------------------------------------


@dataclass
class GauntletResult:
    """Deterministic verification. Nothing subjective reaches this."""

    tests_pass: bool = False
    lint_pass: bool = False
    types_pass: bool = False
    score_before: float = 0.0
    score_after: float = 0.0
    file_mass_before: float = 0.0
    file_mass_after: float = 0.0
    functions_before: int = 0
    functions_after: int = 0
    single_caller_helpers: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def improved(self) -> bool:
        return self.score_after < self.score_before

    @property
    def shredded(self) -> bool:
        """Did the model split the function up without making the file simpler?

        The failure docs/metrics.md §10.5 predicts: a ceiling on one function, met by
        scattering its complexity across twenty one-line helpers. Total complexity mass
        staying put while function count jumps is that signature.
        """
        added = self.functions_after - self.functions_before
        mass_kept = self.file_mass_after >= self.file_mass_before * 0.92
        return added >= 3 and mass_kept

    @property
    def passed(self) -> bool:
        return (
            self.tests_pass
            and self.lint_pass
            and self.types_pass
            and self.improved
            and not self.shredded
        )


def run_gauntlet(sandbox: Sandbox, target: Target, before: dict[str, float]) -> GauntletResult:
    """Verify a candidate deterministically, before any model is asked an opinion."""
    result = GauntletResult(
        score_before=before["score"],
        file_mass_before=before["mass"],
        functions_before=int(before["functions"]),
    )

    tests = sandbox.run("-m", "pytest", "-m", "not oracle and not llm", "-q", "-x")
    result.tests_pass = tests.returncode == 0
    if not result.tests_pass:
        result.failures.append(_tail(tests.stdout or tests.stderr))

    lint = sandbox.run("-m", "ruff", "check", ".")
    result.lint_pass = lint.returncode == 0
    if not result.lint_pass:
        result.failures.append(_tail(lint.stdout))

    types = sandbox.run("-m", "mypy")
    result.types_pass = types.returncode == 0
    if not result.types_pass:
        result.failures.append(_tail(types.stdout))

    after = measure(sandbox, target)
    result.score_after = after["score"]
    result.file_mass_after = after["mass"]
    result.functions_after = int(after["functions"])
    result.single_caller_helpers = int(after["single_caller"])
    return result


def measure(sandbox: Sandbox | None, target: Target) -> dict[str, float]:
    """Score, total complexity mass and function count for the target's file."""
    base = sandbox.path if sandbox else ROOT
    interpreter = str(sandbox.python) if sandbox else sys.executable
    result = subprocess.run(
        [interpreter, "-m", "oxn", "metrics", "--json", "--limit", "300", target.path],
        cwd=base,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {"score": 999.0, "mass": 0.0, "functions": 0.0, "single_caller": 0.0}
    rows = json.loads(result.stdout).get("entities", [])
    scores = [row["value"] for row in rows]
    target_rows = [row for row in rows if row["qualified_name"].endswith(f".{target.leaf}")]
    return {
        "score": float(target_rows[0]["value"]) if target_rows else 0.0,
        "mass": float(sum(scores)),
        "functions": float(len(rows)),
        "single_caller": 0.0,
    }


def _tail(text: str, lines: int = 12) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


# ---- the actor ----------------------------------------------------------------------------

ACTOR_SYSTEM = """You are refactoring Python for readability, not for a metric.
You reply with one complete Python function definition and nothing else: no prose, no
markdown fences, no surrounding code."""

ACTOR_PROMPT = """This function exceeds a cognitive-complexity ceiling of {ceiling}.
It currently scores {score}.

The tool's breakdown of where the score comes from:
{trail}

Rules you must follow, because the point is readable code and not a lower number:
- Do NOT split the function into several small single-use helpers. Complexity moved
  somewhere else is not complexity removed, and it is detected and rejected.
- DO reduce nesting: early returns and guard clauses, flattening `else` after `return`,
  replacing nested conditionals with a dispatch table or a lookup where that reads better.
- Preserve behaviour exactly. Every existing test must still pass.
- Keep the signature, the name, and any decorators identical.
- Keep the docstring, updating it only if the behaviour description would otherwise be wrong.
- Match the surrounding code's style. Comments explain *why*, never *what*.

Here is the whole file for context:

```python
{file_source}
```

Reply with the complete replacement for `{name}` only.
"""


def ask_actor(client: Any, target: Target, ceiling: int, file_source: str) -> str:
    prompt = ACTOR_PROMPT.format(
        ceiling=ceiling,
        score=int(target.score),
        trail="\n".join(f"  {line}" for line in target.trail) or "  (unavailable)",
        file_source=file_source,
        name=target.leaf,
    )
    reply = client.generate(prompt, system=ACTOR_SYSTEM)
    return _strip_fences(reply)


def _strip_fences(text: str) -> str:
    cleaned = text.strip()
    if "```" in cleaned:
        parts = cleaned.split("```")
        for part in parts[1:]:
            body = part[7:] if part.startswith("python\n") else part
            if "def " in body:
                return body.strip("\n")
    return cleaned


# ---- the judge ----------------------------------------------------------------------------

JUDGE_SYSTEM = """You review refactorings. You answer only with a JSON object."""

JUDGE_PROMPT = """A function was refactored to reduce cognitive complexity from {before} to
{after}. All tests, type checks and lint already pass; you are not re-checking those.

Judge whether this is a real simplification or merely a rearrangement.

BEFORE:
```python
{original}
```

AFTER:
```python
{candidate}
```

Reply with exactly this JSON object and nothing else:
{{"behaviour_preserved": true or false,
  "genuinely_simpler": true or false,
  "readability": 1 to 5,
  "concerns": ["short specific concern", ...],
  "verdict": "accept" or "reject"}}
"""


def ask_judge(
    client: Any, original: str, candidate: str, before: float, after: float
) -> dict[str, Any]:
    return client.generate_json(
        JUDGE_PROMPT.format(
            before=int(before), after=int(after), original=original, candidate=candidate
        ),
        system=JUDGE_SYSTEM,
    )


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


def repair(targets: list[Target], *, ceiling: int, retries: int, dry_run: bool) -> list[Attempt]:
    """Attempt to repair each target, verifying every candidate before judging it."""
    from oxn.llm import OllamaClient
    from oxn.profiles import profile_for_path

    actor: Any = _FakeClient() if dry_run else OllamaClient.from_env()
    judge: Any = _FakeClient() if dry_run else OllamaClient.judge_from_env()

    DIFFS.mkdir(parents=True, exist_ok=True)
    attempts: list[Attempt] = []

    for target in targets:
        print(f"\n{BOLD}{target.leaf}{RESET} {DIM}{target.path} scores {target.score:.0f}{RESET}")
        before = measure(None, target)
        profile = profile_for_path(target.path)
        if profile is None:
            continue

        sandbox = Sandbox(target.leaf)
        try:
            print(f"{DIM}  creating sandbox...{RESET}")
            sandbox.create()

            for index in range(1, retries + 1):
                started = time.perf_counter()
                original_file = (sandbox.path / target.path).read_bytes()
                span = function_span(sandbox.path / target.path, target.leaf, profile)
                if span is None:
                    print(f"{RED}  cannot locate {target.leaf}{RESET}")
                    break
                original = original_file[span[0] : span[1]].decode()

                try:
                    candidate = ask_actor(
                        actor, target, ceiling, original_file.decode(errors="replace")
                    )
                except Exception as error:  # noqa: BLE001 - a model failure is a data point
                    attempts.append(
                        Attempt(target.leaf, target.path, index, False, {}, error=str(error)[:200])
                    )
                    print(f"{RED}  attempt {index}: actor failed: {error}{RESET}")
                    continue

                patched = original_file[: span[0]] + candidate.encode() + original_file[span[1] :]
                (sandbox.path / target.path).write_bytes(patched)

                gauntlet = run_gauntlet(sandbox, target, before)
                verdict: dict[str, Any] = {}
                if gauntlet.passed:
                    try:
                        verdict = ask_judge(
                            judge, original, candidate, gauntlet.score_before, gauntlet.score_after
                        )
                    except Exception as error:  # noqa: BLE001
                        verdict = {"verdict": "unavailable", "error": str(error)[:200]}

                accepted = gauntlet.passed and verdict.get("verdict") == "accept"
                attempts.append(
                    Attempt(
                        target.leaf,
                        target.path,
                        index,
                        accepted,
                        asdict(gauntlet),
                        verdict,
                        seconds=time.perf_counter() - started,
                    )
                )
                _report_attempt(index, gauntlet, verdict, accepted)

                if accepted:
                    _write_diff(target, original, candidate)
                    break
                # Restore before the next attempt so each starts from the original.
                (sandbox.path / target.path).write_bytes(original_file)
        finally:
            sandbox.destroy()

    _append_log(attempts)
    return attempts


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
    print(
        f"  attempt {index}: " + "  ".join(marks) + f"  judge={decision}  {colour}"
        f"{'ACCEPTED' if accepted else 'rejected'}{RESET}"
    )
    for concern in verdict.get("concerns", [])[:3]:
        print(f"{DIM}      concern: {concern}{RESET}")


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
    print(f"{GREEN}  diff written: {out}{RESET}")


def _append_log(attempts: list[Attempt]) -> None:
    if not attempts:
        return
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as handle:
        for attempt in attempts:
            handle.write(json.dumps({"timestamp": time.time(), **asdict(attempt)}) + "\n")
    print(f"\n{DIM}logged {len(attempts)} attempt(s) to {LOG.relative_to(ROOT)}{RESET}")


class _FakeClient:
    """A deterministic stand-in, so the harness itself has tests that need no model."""

    model = "dry-run"

    def generate(self, prompt: str, *, temperature: float = 0.0, system: str = "") -> str:
        del prompt, temperature, system
        return "def placeholder():\n    return None\n"

    def generate_json(self, prompt: str, *, system: str = "") -> dict[str, object]:
        del prompt, system
        return {"verdict": "reject", "concerns": ["dry run"], "genuinely_simpler": False}


# ---- reporting ------------------------------------------------------------------------------


def summarise() -> None:
    """What the log says about convergence -- the study 2508.11958 asks for."""
    if not LOG.exists():
        print("no attempts logged yet")
        return
    rows = [json.loads(line) for line in LOG.read_text().splitlines() if line.strip()]
    by_target: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_target.setdefault(row["target"], []).append(row)

    converged = sum(1 for group in by_target.values() if any(r["accepted"] for r in group))
    print(f"{BOLD}{len(by_target)} target(s), {len(rows)} attempt(s){RESET}")
    print(f"  converged: {converged}/{len(by_target)}")
    for name, group in sorted(by_target.items()):
        best = min(
            (r["gauntlet"].get("score_after", 999) for r in group if r["gauntlet"]), default=None
        )
        first = group[0]["gauntlet"].get("score_before") if group[0]["gauntlet"] else None
        status = (
            f"{GREEN}accepted{RESET}" if any(r["accepted"] for r in group) else f"{RED}no{RESET}"
        )
        print(f"  {name:32} {first} -> {best}  attempts={len(group)}  {status}")

    judged = [r for r in rows if r.get("judge", {}).get("verdict") in {"accept", "reject"}]
    if judged:
        agree = sum(
            1
            for r in judged
            if (r["judge"]["verdict"] == "accept") == bool(r["gauntlet"].get("tests_pass"))
        )
        print(f"\n  judge/gauntlet agreement: {agree}/{len(judged)}")


def plan(ceiling: int, limit: int) -> None:
    for target in select_targets(ceiling, limit, skip=set()):
        print(f"  {target.score:5.0f}  {target.leaf:32} {DIM}{target.path}{RESET}")
        for line in target.trail[:3]:
            print(f"{DIM}          {line}{RESET}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["plan", "repair", "report"])
    parser.add_argument("--ceiling", type=int, default=12)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true", help="exercise the loop with no model")
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
        print("nothing over the ceiling")
        return 0
    attempts = repair(targets, ceiling=args.ceiling, retries=args.retries, dry_run=args.dry_run)
    return 0 if any(a.accepted for a in attempts) or args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
