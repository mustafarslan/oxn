"""What the harness says to a model, and what it accepts back.

The prompts are the experiment. `NO_EXTRACTION` and `ALLOW_EXTRACTION` are two arms of a
controlled comparison and differ in exactly one rule, which `tests/test_dogfood.py` asserts
byte for byte -- otherwise it is not a comparison.

Extraction of the reply is here too, and it has been wrong twice: once returning only the
first fenced block of a multi-block answer, and once treating an empty completion as an empty
function. Both silently deleted the code they were meant to repair, and both are pinned by
tests now.

Split out of `dogfood.py` because that file passed its own file ceiling. The seam: this
module knows about models and nothing about sandboxes; `gauntlet.py` is the reverse.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from arms import Arm
    from dogfood import Target
    from gauntlet import GauntletResult


# ---- the actor ----------------------------------------------------------------------------

#: The system prompt, per language. It said "Python" for every bed -- and the harness has
#: six, so a Go repair was asked for "one complete Python function definition" with the Go
#: source inside a ```python fence. The whole point of the bed set is that a result measured
#: only on Python says nothing about the five languages whose metrics were fixed this week.
ACTOR_SYSTEM = """You are refactoring {language} for readability, not for a metric.
You reply with one complete {language} {unit} and nothing else: no prose, no
markdown fences, no surrounding code."""

#: What each language calls the thing being replaced, and the fence its source belongs in.
#: `oxn.profiles` knows the language for a path; the words are this file's business.
_UNIT = {
    "python": "function definition",
    "javascript": "function declaration",
    "typescript": "function declaration",
    "go": "function declaration",
    "rust": "function item",
    "java": "method declaration",
}

ACTOR_PROMPT = """This function exceeds a cognitive-complexity ceiling of {ceiling}.
It currently scores {score}.
{context}{guidance}
Here is the whole file for context:

```{language}
{file_source}
```

Reply with the complete replacement for `{name}` only. It must contain a
`def {name}(...)` -- a reply that omits it deletes the function.
{feedback}"""

#: OXN's own account of where the score comes from -- the increment trail
#: `explain_violation` returns. The `mcp` arm is this channel and nothing else.
CONTEXT_BLOCK = """
The tool's breakdown of where the score comes from:
{trail}
"""

#: The rules `oxn init` writes into `CLAUDE.md`. The `claude-md` arm is this channel alone,
#: which is the arm that asks whether written guidance survives contact with a metric.
GUIDANCE_BLOCK = """
Rules you must follow, because the point is readable code and not a lower number:
{extraction}- DO reduce nesting: early returns and guard clauses, flattening `else` after `return`,
  replacing nested conditionals with a dispatch table or a lookup where that reads better.
- Preserve behaviour exactly. Every existing test must still pass.
- Keep the signature, the name, and any decorators identical.
- Keep the docstring, updating it only if the behaviour description would otherwise be wrong.
- Match the surrounding code's style. Comments explain *why*, never *what*.
"""


#: The two arms of the extraction experiment. The banning arm was the harness's only
#: behaviour until a live run showed what it costs: on a dispatch function with a dozen
#: irreducible branches, flattening into comprehensions is the sole legal move, and OXN
#: charges comprehension clauses about what the nesting they replace would cost. The ceiling
#: is then unreachable by construction -- so a failure to converge says nothing about the
#: model. It also contradicted the gauntlet, which already polices extraction by measuring
#: the file's total complexity mass, and does it with more precision than a blanket ban.
NO_EXTRACTION = """\
- Do NOT split the function into several small single-use helpers. Complexity moved
  somewhere else is not complexity removed, and it is detected and rejected.
"""

ALLOW_EXTRACTION = """\
- You MAY extract helpers, but each must be worth reading on its own: a name that
  describes a whole idea, and a body that means something away from the call site.
  Scattering the function across many one-line helpers is detected and rejected --
  complexity moved is not complexity removed, and the file's total is measured.
"""

FEEDBACK = """
Your previous attempt was rejected for these reasons. Address them rather than
starting again from a different design:

{failures}
"""


@dataclass(frozen=True, slots=True)
class Ask:
    """One request to the actor: the target, the bar, and what happened last time.

    A record rather than six positional arguments, because these travel together and their
    number was growing -- `allow_extraction` arrived when the prompt became an experiment,
    and `feedback` when a retry stopped being a fresh roll of the dice.
    """

    target: Target
    ceiling: int
    file_source: str
    feedback: str = ""
    allow_extraction: bool = False
    #: Which of OXN's three channels this run opens. `None` means all of them, which is what
    #: the harness did before the arms existed and is what `hybrid` now names.
    arm: Arm | None = None


def ask_actor(client: Any, ask: Ask) -> tuple[str, str, str]:
    """The candidate, the raw reply it was extracted from, and the prompt that produced it.

    All three are kept. The raw reply is the only thing that can diagnose an extraction bug,
    and logging just the extracted candidate is how one went unnoticed. The prompt is
    returned because the arms differ *by prompt* -- an arm's cost is what it asked for, and
    reconstructing it afterwards would measure a second construction rather than the one
    that was sent.
    """
    language = _language_of(ask.target.path)
    prompt = ACTOR_PROMPT.format(
        ceiling=ask.ceiling,
        score=int(ask.target.score),
        context=_context_block(ask),
        guidance=_guidance_block(ask),
        file_source=ask.file_source,
        name=ask.target.leaf,
        language=language,
        feedback=FEEDBACK.format(failures=ask.feedback) if ask.feedback else "",
    )
    system = ACTOR_SYSTEM.format(language=language, unit=_UNIT.get(language, "function definition"))
    try:
        reply = client.generate(prompt, system=system)
    except Exception as error:
        # The prompt is the only thing the caller cannot reconstruct -- rebuilding it would
        # measure a second construction rather than the one that was sent -- and a failed
        # ask is exactly where its cost was being lost. Attached rather than wrapped, so the
        # exception keeps the type the harness dispatches on.
        error.prompt_chars = len(prompt)  # type: ignore[attr-defined]
        raise
    return _strip_fences(reply, ask.target.leaf), reply, prompt


def _language_of(path: str) -> str:
    """The bed's language, from the target's own path.

    Asked of `oxn.profiles`, which is the single answer to "what is this file" -- the same
    table the walk reads, so a bed cannot be analysed as one language and prompted as
    another. Falls back to Python only because a prompt with no language at all reads worse
    than a wrong one, and no bed reaches it: every target came from a profiled file.
    """
    from oxn.profiles import profile_for_path

    profile = profile_for_path(path)
    return profile.name if profile is not None else "python"


def _context_block(ask: Ask) -> str:
    """The score's breakdown, as much of it as this arm opens.

    An arm without the context channel sends none: the actor is told the function is over
    budget and left to find out why, which is what "no MCP" means.
    """
    lines = list(ask.target.trail)
    if ask.arm is not None:
        lines = ask.arm.trail(lines)
        if not ask.arm.context:
            return ""
    body = "\n".join(f"  {line}" for line in lines) or "  (unavailable)"
    return CONTEXT_BLOCK.format(trail=body)


def _guidance_block(ask: Ask) -> str:
    """The written rules, or nothing where the arm closes that channel."""
    if ask.arm is not None and not ask.arm.guidance:
        return ""
    return GUIDANCE_BLOCK.format(
        extraction=ALLOW_EXTRACTION if ask.allow_extraction else NO_EXTRACTION
    )


#: The checks that either passed or did not, and what to say when they did not. A table
#: rather than a run of `if`s: every entry is the same shape, and the shape is the point.
_CHECKS: tuple[tuple[str, str], ...] = (
    ("target_present", "- Your reply did not define the function, so it was deleted."),
    ("tests_pass", "- The test suite failed."),
    ("lint_pass", "- Lint failed."),
    ("types_pass", "- Type checking failed."),
)


def _feedback(gauntlet: GauntletResult) -> str:
    """What to tell the actor next time.

    Retrying with an identical prompt is resampling, not iteration: at temperature 0 the
    only thing that varies is the model's own nondeterminism. The convergence question this
    harness exists to ask (arXiv 2508.11958) is about a *feedback* loop, so the loop has to
    close.
    """
    reasons = [message for attribute, message in _CHECKS if not getattr(gauntlet, attribute)]
    reasons.extend(
        message
        for message in (_ceiling_note(gauntlet), _helper_note(gauntlet), _shred_note(gauntlet))
        if message
    )
    detail = "\n".join(gauntlet.failures[:2])
    return "\n".join(reasons) + (f"\n\nTool output:\n{detail}" if detail else "")


def _ceiling_note(gauntlet: GauntletResult) -> str:
    """Progress is not arrival, and the actor is told the difference."""
    if not gauntlet.target_present or gauntlet.score_after <= gauntlet.ceiling:
        return ""
    return (
        f"- It still scores {gauntlet.score_after:.0f}, above the ceiling of "
        f"{gauntlet.ceiling:.0f}. Reducing the score is not enough; it must reach the ceiling."
    )


def _helper_note(gauntlet: GauntletResult) -> str:
    """Clearing the ceiling by writing the next run's violation is not converging."""
    over = {
        name.rsplit(".", 1)[-1]: score
        for name, score in gauntlet.new_helpers.items()
        if score > gauntlet.ceiling
    }
    if not over:
        return ""
    listed = ", ".join(f"{name} ({score:.0f})" for name, score in sorted(over.items()))
    return (
        f"- A helper you added is itself over the ceiling of {gauntlet.ceiling:.0f}: "
        f"{listed}. Moving the problem into a new function does not solve it."
    )


def _shred_note(gauntlet: GauntletResult) -> str:
    if not gauntlet.shredded:
        return ""
    return (
        f"- The function was scattered into {len(gauntlet.new_helpers)} helpers that do "
        f"almost nothing individually. Extract units of work with names worth reading, not "
        f"single lines."
    )


def _strip_fences(text: str, name: str = "") -> str:
    """The code in a fenced reply -- one answer, not every draft the model wrote.

    Two shapes have to work, and they need opposite handling:

    * a short reply where one block holds a helper and the next the rewritten function.
      Both are wanted, so blocks are concatenated. Returning only the first was an earlier
      bug: the target was dropped and its callers raised `NameError`.
    * a reasoning dump, where the model thinks in prose and leaves a trail of drafts.
      Measured on a real reply: 131,094 characters, **105 fenced blocks, six of which
      define the target**. Concatenating those produced an unparseable file -- six
      competing definitions and a string literal cut off mid-draft -- which the gauntlet
      then reported as a failed refactoring rather than as a failure to extract one.

    They are told apart by counting: when several blocks define the target, the reply is a
    draft sequence and only one of them is the answer. The one chosen is the **last that
    parses**, which is a check rather than a guess -- on that same reply the final draft
    was itself syntactically broken, so "last" alone would not have been enough.

    Splitting on the fence marker alternates outside/inside, so only the odd-numbered
    segments are code; the even ones are prose, which must never reach a source file.
    """
    cleaned = text.strip()
    if "```" not in cleaned:
        return cleaned
    blocks = [_fence_body(part) for part in cleaned.split("```")[1::2]]
    blocks = [block for block in blocks if "def " in block]
    if not blocks:
        return cleaned

    drafts = [block for block in blocks if name and _defines(block, name)]
    if len(drafts) > 1:
        return _last_usable(drafts)
    return "\n\n\n".join(blocks)


def _fence_body(part: str) -> str:
    """Drop the language tag a fence may open with (```python)."""
    head, _, rest = part.partition("\n")
    return (rest if head.strip().isalpha() else part).strip("\n")


def _last_usable(drafts: list[str]) -> str:
    """The last draft that is syntactically valid Python, else the last one.

    Falling back to the last rather than to nothing keeps the failure legible: a candidate
    that does not compile is rejected by the gauntlet with the syntax error attached, which
    says more than "no code found".
    """
    for draft in reversed(drafts):
        try:
            ast.parse(draft)
        except SyntaxError:
            continue
        return draft
    return drafts[-1]


def _defines(source: str, name: str, profile: Any = None) -> bool:
    """Does this candidate actually define the function it is replacing?

    Checked before the gauntlet runs, because a candidate that omits the target deletes it
    on splice -- and finding that out costs a full test, lint and type run first.

    **This was `def {name}(` and nothing else, so it rejected every non-Python reply.** The
    system prompt above carries a note about having once said "Python" for all six beds; that
    was fixed and this was not, which left the harness asking a Go model for Go and then
    refusing the answer because it did not look like Python. Go's `func`, Rust's `fn`, Java's
    and TypeScript's method syntax never match, so four of the six declared beds could not
    accept a repair no matter what the model wrote -- measured on go-kit, six attempts across
    two targets, every one rejected here before the gauntlet ran.

    Parsed rather than pattern-matched, using the same `build_file` the graph builder uses, so
    "does this define X" is answered by the thing that decides what an entity is everywhere
    else. The regex remains for a caller with no profile, which is the tests.
    """
    if profile is None:
        return re.search(rf"^\s*(?:async\s+)?def\s+{re.escape(name)}\s*\(", source, re.M) is not None

    from oxn.graph.builder import build_file
    from oxn.languages import get_parser

    raw = source.encode()
    root = get_parser(profile.grammar).parse(raw).root_node
    parsed = build_file(f"candidate{next(iter(sorted(profile.extensions)))}", raw, profile, root)
    return any(
        entity.name == name and entity.kind.value in {"function", "method"}
        for entity in parsed.entities
    )


# ---- the judge ----------------------------------------------------------------------------

JUDGE_SYSTEM = """You review refactorings. You answer only with a JSON object."""

JUDGE_PROMPT = """A function was refactored to reduce cognitive complexity from {before} to
{after}. All tests, type checks and lint already pass; you are not re-checking those.

Judge whether this is a real simplification or merely a rearrangement.

BEFORE:
```{language}
{original}
```

AFTER:
```{language}
{candidate}
```

Reply with exactly this JSON object and nothing else:
{{"behaviour_preserved": true or false,
  "genuinely_simpler": true or false,
  "readability": 1 to 5,
  "concerns": ["short specific concern", ...],
  "verdict": "accept" or "reject"}}
"""


@dataclass(frozen=True, slots=True)
class Review:
    """One refactoring put to the judge: both versions, the scores, and the language.

    A record for the same reason `Ask` is one -- these travel together and `language` was the
    sixth. It arrived because the judge's fence said ```python for every bed, the way the
    actor's system prompt once did: that was fixed and this was not, so a Go refactoring was
    shown to the judge labelled as Python. The same half-fix left `_defines` matching `def`
    only, which rejected every non-Python reply before the gauntlet ever ran.
    """

    original: str
    candidate: str
    before: float
    after: float
    language: str = "python"


def ask_judge(client: Any, review: Review) -> dict[str, Any]:
    verdict: dict[str, Any] = client.generate_json(
        JUDGE_PROMPT.format(
            before=int(review.before),
            after=int(review.after),
            original=review.original,
            candidate=review.candidate,
            language=review.language,
        ),
        system=JUDGE_SYSTEM,
    )
    return verdict
