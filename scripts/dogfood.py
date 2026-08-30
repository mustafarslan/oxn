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
import os
import re
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
    #: Does the function the actor was asked to simplify still exist? A deleted function
    #: is not a simplified one, and it used to score zero -- see `measure`.
    target_present: bool = True
    #: The ceiling the repair had to meet. Getting closer is not the same as arriving.
    ceiling: float = 0.0

    @property
    def improved(self) -> bool:
        return self.score_after < self.score_before

    @property
    def under_ceiling(self) -> bool:
        return self.score_after <= self.ceiling

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
        """Every one of these is load-bearing, and two were added after a live run.

        `target_present` closes a soundness hole the harness had from the start: an actor
        that *deletes* the function scores zero on it, which `improved` reads as the best
        repair ever produced. Only the test suite caught it, and only because that function
        happened to be covered -- an uncovered one would have been accepted.

        `under_ceiling` is the difference between progress and success. A 35 taken to 14 is
        real work, but OXN would still block it, and a harness that accepts what the tool
        rejects is measuring the wrong thing.
        """
        return (
            self.tests_pass
            and self.lint_pass
            and self.types_pass
            and self.target_present
            and self.improved
            and self.under_ceiling
            and not self.shredded
        )


def run_gauntlet(
    sandbox: Sandbox, target: Target, before: dict[str, float], ceiling: int
) -> GauntletResult:
    """Verify a candidate deterministically, before any model is asked an opinion."""
    result = GauntletResult(
        score_before=before["score"],
        file_mass_before=before["mass"],
        functions_before=int(before["functions"]),
        ceiling=float(ceiling),
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
    result.target_present = bool(after["present"])
    if not result.target_present:
        result.failures.append(
            f"{target.leaf} no longer exists in {target.path}. The task is to simplify it, "
            f"not to remove it; every caller still expects it."
        )
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
        # The file no longer parses. 999 keeps this out of every comparison that treats a
        # lower score as better.
        return {"score": 999.0, "mass": 0.0, "functions": 0.0, "single_caller": 0.0, "present": 0.0}
    rows = json.loads(result.stdout).get("entities", [])
    scores = [row["value"] for row in rows]
    target_rows = [row for row in rows if row["qualified_name"].endswith(f".{target.leaf}")]
    return {
        # A missing function is reported as missing, never as a score. Returning 0.0 here --
        # as this did until a live run deleted `_imported_names` outright -- makes deletion
        # look like the strongest possible refactoring.
        "score": float(target_rows[0]["value"]) if target_rows else 999.0,
        "mass": float(sum(scores)),
        "functions": float(len(rows)),
        "single_caller": 0.0,
        "present": 1.0 if target_rows else 0.0,
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

Reply with the complete replacement for `{name}` only. It must contain a
`def {name}(...)` -- a reply that omits it deletes the function.
{feedback}"""


FEEDBACK = """
Your previous attempt was rejected for these reasons. Address them rather than
starting again from a different design:

{failures}
"""


def ask_actor(
    client: Any, target: Target, ceiling: int, file_source: str, feedback: str = ""
) -> tuple[str, str]:
    """The candidate, and the raw reply it was extracted from.

    Both are kept: the raw reply is the only thing that can diagnose an extraction bug, and
    logging just the extracted candidate is how one went unnoticed.
    """
    prompt = ACTOR_PROMPT.format(
        ceiling=ceiling,
        score=int(target.score),
        trail="\n".join(f"  {line}" for line in target.trail) or "  (unavailable)",
        file_source=file_source,
        name=target.leaf,
        feedback=FEEDBACK.format(failures=feedback) if feedback else "",
    )
    reply = client.generate(prompt, system=ACTOR_SYSTEM)
    return _strip_fences(reply), reply


def _feedback(gauntlet: GauntletResult) -> str:
    """What to tell the actor next time.

    Retrying with an identical prompt is resampling, not iteration: at temperature 0 the
    only thing that varies is the model's own nondeterminism. The convergence question this
    harness exists to ask (arXiv 2508.11958) is about a *feedback* loop, so the loop has to
    close.
    """
    reasons = []
    if not gauntlet.target_present:
        reasons.append("- Your reply did not define the function, so it was deleted.")
    if not gauntlet.tests_pass:
        reasons.append("- The test suite failed.")
    if not gauntlet.lint_pass:
        reasons.append("- Lint failed.")
    if not gauntlet.types_pass:
        reasons.append("- Type checking failed.")
    if gauntlet.target_present and not gauntlet.under_ceiling:
        reasons.append(
            f"- It still scores {gauntlet.score_after:.0f}, above the ceiling of "
            f"{gauntlet.ceiling:.0f}. Reducing the score is not enough; it must reach the "
            f"ceiling."
        )
    if gauntlet.shredded:
        reasons.append(
            "- The complexity was scattered into new helpers rather than removed; the "
            "file's total is unchanged."
        )
    detail = "\n".join(gauntlet.failures[:2])
    return "\n".join(reasons) + (f"\n\nTool output:\n{detail}" if detail else "")


def _strip_fences(text: str) -> str:
    """Every fenced code block in the reply, concatenated in order.

    This used to return the *first* block containing `def `, which silently discarded the
    rest. A model that writes a helper in one block and the rewritten function in the next
    -- an entirely reasonable way to answer -- would have the second block dropped, and the
    first spliced over the target's byte range. The function then does not exist, and its
    callers raise `NameError`.

    Splitting on the fence marker alternates outside/inside, so only the odd-numbered
    segments are code; the even ones are the model's prose, which must never be spliced
    into a source file.
    """
    cleaned = text.strip()
    if "```" not in cleaned:
        return cleaned
    blocks = []
    for part in cleaned.split("```")[1::2]:
        body = part.split("\n", 1)[1] if part.split("\n", 1)[0].strip().isalpha() else part
        if "def " in body:
            blocks.append(body.strip("\n"))
    return "\n\n\n".join(blocks) if blocks else cleaned


def _defines(source: str, name: str) -> bool:
    """Does this candidate actually define the function it is replacing?

    Checked before the gauntlet runs, because a candidate that omits the target deletes it
    on splice -- and finding that out costs a full test, lint and type run first.
    """
    return re.search(rf"^\s*(?:async\s+)?def\s+{re.escape(name)}\s*\(", source, re.M) is not None


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
    verdict: dict[str, Any] = client.generate_json(
        JUDGE_PROMPT.format(
            before=int(before), after=int(after), original=original, candidate=candidate
        ),
        system=JUDGE_SYSTEM,
    )
    return verdict


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


def repair(
    targets: list[Target],
    *,
    ceiling: int,
    retries: int,
    dry_run: bool,
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

            feedback = ""
            for index in range(1, retries + 1):
                started = time.perf_counter()
                original_file = (sandbox.path / target.path).read_bytes()
                span = function_span(sandbox.path / target.path, target.leaf, profile)
                if span is None:
                    say(f"{RED}  cannot locate {target.leaf}{RESET}")
                    break
                original = original_file[span[0] : span[1]].decode()

                try:
                    candidate, reply = ask_actor(
                        actor, target, ceiling, original_file.decode(errors="replace"), feedback
                    )
                except Exception as error:  # noqa: BLE001 - a model failure is a data point
                    attempts.append(
                        Attempt(target.leaf, target.path, index, False, {}, error=str(error)[:200])
                    )
                    say(f"{RED}  attempt {index}: actor failed: {error}{RESET}")
                    continue

                if not _defines(candidate, target.leaf):
                    # Splicing this would delete the function. Reject now rather than after
                    # a full test, lint and type run.
                    feedback = f"- Your reply did not contain a `def {target.leaf}(...)`."
                    attempts.append(
                        Attempt(
                            target.leaf,
                            target.path,
                            index,
                            False,
                            {},
                            error=f"the reply did not define {target.leaf}",
                            seconds=time.perf_counter() - started,
                            candidate=candidate,
                            reply=reply,
                        )
                    )
                    say(f"{RED}  attempt {index}: reply did not define {target.leaf}{RESET}")
                    continue

                patched = original_file[: span[0]] + candidate.encode() + original_file[span[1] :]
                (sandbox.path / target.path).write_bytes(patched)

                gauntlet = run_gauntlet(sandbox, target, before, ceiling)
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
                        candidate=candidate,
                        reply=reply,
                    )
                )
                _report_attempt(index, gauntlet, verdict, accepted)
                feedback = _feedback(gauntlet)

                if accepted:
                    _write_diff(target, original, candidate)
                    break
                # Restore before the next attempt so each starts from the original.
                (sandbox.path / target.path).write_bytes(original_file)
        finally:
            sandbox.destroy()

    if dry_run:
        # The fake client emits fixed junk, so these rows would be indistinguishable from
        # real attempts in a benchmark record that exists to be read later.
        say(f"\n{DIM}dry run -- not logged{RESET}")
    else:
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
        retries=args.retries,
        dry_run=args.dry_run,
        model=args.model,
        judge_model=args.judge_model,
        host=args.host,
    )
    return 0 if any(a.accepted for a in attempts) or args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
