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

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from dogfood import Target
    from gauntlet import GauntletResult


# ---- the actor ----------------------------------------------------------------------------

ACTOR_SYSTEM = """You are refactoring Python for readability, not for a metric.
You reply with one complete Python function definition and nothing else: no prose, no
markdown fences, no surrounding code."""

ACTOR_PROMPT = """This function exceeds a cognitive-complexity ceiling of {ceiling}.
It currently scores {score}.

The tool's breakdown of where the score comes from:
{trail}

Rules you must follow, because the point is readable code and not a lower number:
{extraction}- DO reduce nesting: early returns and guard clauses, flattening `else` after `return`,
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


def ask_actor(client: Any, ask: Ask) -> tuple[str, str]:
    """The candidate, and the raw reply it was extracted from.

    Both are kept: the raw reply is the only thing that can diagnose an extraction bug, and
    logging just the extracted candidate is how one went unnoticed.
    """
    prompt = ACTOR_PROMPT.format(
        ceiling=ask.ceiling,
        score=int(ask.target.score),
        trail="\n".join(f"  {line}" for line in ask.target.trail) or "  (unavailable)",
        file_source=ask.file_source,
        name=ask.target.leaf,
        feedback=FEEDBACK.format(failures=ask.feedback) if ask.feedback else "",
        extraction=ALLOW_EXTRACTION if ask.allow_extraction else NO_EXTRACTION,
    )
    reply = client.generate(prompt, system=ACTOR_SYSTEM)
    return _strip_fences(reply), reply


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
