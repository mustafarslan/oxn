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
applied to whatever comes back, so a model that invents a number loses the right to phrase the
summary. It does not cost the review: `review_body` here writes one from the measurement alone,
which is what an Actions runner with no model host posts and what a refusal falls back to.
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

    Refusing produces no *prose* rather than prose with a warning attached. A reader who sees
    sentences does not audit them -- that is the whole reason the numbers come from the
    measurement -- so a comment saying "some of these figures may be invented" is worse than
    none. What the pull request gets instead is `review_body`: OXN's own summary, which cannot
    state an unmeasured number because it composes none. `report.run_review_report` makes that
    substitution and records `invented` beside it, so the fallback is never read as an
    endorsement.
    """
    from oxn.review import quotable_numbers, unquotable

    # `quotable` is dropped from both halves, and from the prompt for a different reason than
    # from the check. Deriving `allowed` from it would make the rule circular. Showing it to
    # the writer would hand a model a bag of loose integers and an instruction to use numbers
    # from a list, which is how numerically-valid nonsense gets written: the numbers belong to
    # the findings they were measured on, and the writer should quote them from there. It stays
    # in the JSON `oxn review` prints, where a person auditing a refusal wants exactly that bag.
    measurement = {key: value for key, value in payload.items() if key != "quotable"}
    allowed = quotable_numbers(measurement)
    prompt = _prompt(measurement)
    invented: Sequence[str] = ()
    for attempt in range(1, attempts + 1):
        body = _attempt(writer, prompt)
        invented = unquotable(body, allowed)
        if body and not invented:
            return ReviewComment(body=body, model=writer.model, attempts=attempt)
        prompt = _retry(measurement, body, invented)
    return ReviewComment(invented=tuple(invented), model=writer.model, attempts=attempts)


def _attempt(writer: Writer, prompt: str) -> str:
    """One generation. A reply that failed to *arrive* is an empty reply, not an exception.

    **`ReplyCutOff` used to abandon both attempts.** A model that hits the token limit has
    produced a bad reply, which is exactly the case the retry exists for -- and measured on
    `glm-5.3:cloud` writing review prose, it happens on roughly one review in five. Raising
    meant the second attempt, the one that usually succeeds, never ran.

    `OllamaUnavailable` is deliberately *not* caught here: an unreachable or throttled host
    will still be unreachable a second later, so retrying spends the timeout twice to reach the
    same answer. `report.run_review_report` catches it one level up and posts OXN's own
    comment instead.
    """
    from oxn.llm import OllamaError, OllamaUnavailable

    try:
        return writer.generate(prompt, system=SYSTEM).strip()
    except OllamaUnavailable:
        raise
    except OllamaError:
        return ""


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


def review_body(payload: dict[str, Any]) -> str:
    """The comment OXN writes itself, from the measurement and nothing else.

    **A writer that cannot invent a number**, because it never composes one: every figure here
    is read straight out of the payload. That makes it the floor rather than a fallback --
    `oxn review` has something to post with no model, no network and no Ollama host, which is
    the situation every GitHub Actions runner is in by default.

    It is also the answer to what a refusal costs. `write_review` refusing means the *prose* is
    lost, not the review: `report.run_review_report` keeps this body and records which numbers
    cost the model its turn. A model earns the right to phrase the summary; it is not the only
    thing that can produce one.
    """
    counts, files = payload["counts"], payload["files"]
    lines = [
        f"**OXN** — {counts['new']} new, {counts['regression']} regressed, "
        f"{counts['baselined']} pre-existing.",
        "",
        f"`{payload['base']}...{payload['head']}`: {files['changed']} files changed, "
        f"{files['measured']} measured, {files['excluded']} excluded.",
    ]
    actionable = [row for row in payload["findings"] if row["origin"] != "baselined"]
    if actionable:
        lines += [
            "",
            *(f"- `{row['path']}:{row['line']}` — {row['message']}" for row in actionable),
        ]
    if files.get("errored"):
        lines += ["", f"{files['errored']} changed files could not be measured."]
    lines += ["", _footer(payload["measured"])]
    return "\n".join(lines)


def _footer(stamp: dict[str, Any]) -> str:
    """Which commit these numbers came from, so a comment can be recognised as stale.

    A pull request gets pushed to and the comment stays where it was. `oxn.review._stamp`
    makes the argument in full; this is the one line of it a reader sees.
    """
    commit = stamp.get("short") or stamp.get("commit", "")
    version = stamp.get("oxn_version", "")
    return f"<sub>Measured on `{commit or 'unknown'}` by oxn {version}. This does not gate.</sub>"
