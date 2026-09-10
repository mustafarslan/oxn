"""Who turns a measurement into a sentence, and the refusal that keeps them honest.

`scripts/actors.py` had this shape first, for P11's arms, and the roadmap points at it as the
prior art. It could not simply be extended: a GitHub Actions job runs `pip install oxn` and
gets `src/oxn/`, so a review that imports from `scripts/` cannot run where reviews happen.
The protocol lives here and that file keeps its registry of repair actors.

**Only Ollama is exercised.** It is the one this repository can run and measure, and the
`OXN_OLLAMA_MODEL` default reaches whatever the host serves. Anthropic, OpenAI and Gemini are
named by the roadmap and are *not* implemented here rather than implemented untested: a client
that has never made a request is not evidence that it works, and shipping four of those would
say OXN supports four providers when it supports one. `Writer` is the seam they slot into, and
`write_review` needs nothing from a backend but `generate`.

The refusal is the interesting part and it belongs to no backend. `review.unquotable` is
applied to whatever comes back, so a model that invents a number produces no comment at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

#: What the writer is told it is, and the one rule it must not break. Deliberately blunt about
#: the numbers: the check that follows is mechanical, so a model that ignores this produces a
#: refusal rather than a wrong comment, and the instruction is what keeps that rare.
SYSTEM = """You write short pull-request review comments for OXN, a code-quality gate.

You are given a JSON measurement. Write GitHub-flavoured Markdown summarising it for the
author.

Rules, in order of importance:
1. Every number you write MUST appear in the measurement. Do not compute totals, averages,
   percentages or differences of your own. Do not write version numbers, dates, or counts
   that are not in the JSON. If you cannot say something without a number that is not there,
   say it without the number.
2. Lead with findings whose "origin" is "new" or "regression". Mention "baselined" findings
   only as pre-existing debt, and never as something this pull request introduced.
3. Quote each finding as `path:line` and name the rule.
4. Be brief. No preamble, no restating these instructions, no offer to help further.
5. If there are no findings, say so in one sentence."""


class Writer(Protocol):
    """What `write_review` needs from whatever is answering it.

    `model` travels with the answer for the same reason `scripts.actors.Actor` carries it: a
    comment whose author is not identifiable is not attributable, and a review posted by a
    model nobody can name is worse than no review.
    """

    # A property rather than a bare annotation: `OllamaClient` is a frozen dataclass, and a
    # protocol declaring `model: str` demands a *settable* attribute, which a frozen one is
    # not. Nothing here assigns to it.
    @property
    def model(self) -> str: ...

    def generate(self, prompt: str, *, temperature: float = 0.0, system: str = "") -> str: ...


@dataclass
class FakeWriter:
    """A writer that returns what it was handed. The suite's, and a workflow's dry run.

    Not a mock of an LLM -- a *substitute* for one, which is what lets the honesty check be
    tested against prose that is known to be dishonest. `replies` is consumed in order.
    """

    model: str = "fake"
    replies: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)

    def generate(self, prompt: str, *, temperature: float = 0.0, system: str = "") -> str:
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else ""


@dataclass(frozen=True, slots=True)
class ReviewComment:
    """A comment, or the reason there is not one."""

    #: The Markdown to post. Empty when `refused` explains why there is nothing to post.
    body: str = ""
    #: Numbers the writer stated that the measurement does not contain. Non-empty means the
    #: comment was refused: see `write_review`.
    invented: tuple[str, ...] = ()
    model: str = ""
    attempts: int = 0

    @property
    def refused(self) -> bool:
        return not self.body

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "REFUSED" if self.refused else "OK",
            "body": self.body,
            "invented": list(self.invented),
            "model": self.model,
            "attempts": self.attempts,
        }


def write_review(payload: dict[str, Any], writer: Writer, *, attempts: int = 2) -> ReviewComment:
    """Turn a measurement into a comment, refusing one that states a number OXN did not.

    **The refusal is not a retry loop dressed up.** The second attempt is given the numbers it
    invented and told to remove them, because the commonest failure is one arithmetic aside in
    an otherwise accurate paragraph and throwing that away serves nobody. But there is no
    third: a model that keeps inventing numbers after being shown them is not going to stop,
    and a review tool whose honesty depends on how many times it asked is not honest.

    Refusing produces no comment at all rather than a comment with a warning attached. A
    reader who sees prose does not audit it -- that is the whole reason the numbers come from
    the measurement -- so a comment that says "some of these figures may be invented" is worse
    than silence, and silence is at least actionable: `oxn review` exits with the reason.
    """
    from oxn.review import quotable_numbers, unquotable

    allowed = quotable_numbers({key: value for key, value in payload.items() if key != "quotable"})
    prompt = _prompt(payload)
    invented: Sequence[str] = ()
    for attempt in range(1, attempts + 1):
        body = writer.generate(prompt, system=SYSTEM).strip()
        invented = unquotable(body, allowed)
        if body and not invented:
            return ReviewComment(body=body, model=writer.model, attempts=attempt)
        prompt = _retry(payload, body, invented)
    return ReviewComment(invented=tuple(invented), model=writer.model, attempts=attempts)


def _prompt(payload: dict[str, Any]) -> str:
    """The measurement, and nothing else.

    No diff, no source. `oxn.review` explains why at length; the short version is that a model
    given code will review the code, and the numbers are the only thing here that was
    measured.
    """
    import json

    return "Measurement:\n\n```json\n" + json.dumps(payload, indent=2, sort_keys=True) + "\n```"


def _retry(payload: dict[str, Any], body: str, invented: Sequence[str]) -> str:
    """Ask again, showing exactly which numbers were not in the measurement."""
    listed = ", ".join(invented) if invented else "(the reply was empty)"
    return (
        f"{_prompt(payload)}\n\n"
        f"Your previous reply was rejected. It contained {listed}, which the measurement above "
        "does not contain. Rewrite it using only numbers that appear in the JSON, or say the "
        "same thing without those numbers.\n\nPrevious reply:\n\n"
        f"{body}"
    )


def ollama_writer(model: str = "") -> Writer:
    """The one backend this repository can actually run. See the module docstring."""
    import dataclasses

    from oxn.llm import OllamaClient

    client = OllamaClient.from_env()
    return dataclasses.replace(client, model=model) if model else client
