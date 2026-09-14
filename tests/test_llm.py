"""Tests that need a language model, run through Ollama.

Configured by `OXN_OLLAMA_HOST` and `OXN_OLLAMA_MODEL`, defaulting to a local Ollama and
`kimi-k3:cloud`. Marked `llm` and skipped when the model will not answer -- unreachable,
throttled, or absent -- so the default lane never depends on a model being up.

**Why so few tests live here.** OXN's metrics are deterministic by design -- `idea.md` §1.1
argues that computing a metric with a model yields an approximation of something a parser
knows exactly, and the whole project rests on that. So a model is not used to *check* a
metric; it is used to check the claims that are genuinely about a model's behaviour.

The claim under test here is the product's central one: that OXN's explanation trail makes a
violation **actionable**, where a bare score does not.
"""

from __future__ import annotations

import pytest

from oxn.graph.builder import build_file
from oxn.languages import get_parser
from oxn.llm import OllamaClient
from oxn.metrics import cognitive_complexity
from oxn.profiles import get_profile

pytestmark = pytest.mark.llm


@pytest.fixture(scope="module")
def model() -> OllamaClient:
    """A model that will actually answer, or a skip saying why not.

    **`available()` answers a different question than this lane needs.** It pings `/api/tags`,
    which reports whether the *host* is up -- and on a machine serving only cloud models that
    listing is empty while every request still routes. So the guard passed and the generate
    came back HTTP 429, failing three tests for a rate limit. A cloud endpoint throttling is an
    environmental condition exactly like an unreachable host, and neither is a defect in OXN.

    So the probe is a real completion. It costs one request per module and it is the only thing
    that answers "will this model reply to me right now".

    It catches `OllamaUnavailable` and not `OllamaError`, which is the difference between "no
    model available to ask" and "the model answered something this client cannot parse". The
    second is a defect and this lane is the only place a live server would show it, so it must
    fail here rather than skip.
    """
    from oxn.llm import OllamaUnavailable

    client = OllamaClient.from_env()
    if not client.available():
        pytest.skip(f"Ollama not reachable at {client.host}")
    try:
        client.generate("Reply with the single word: ok")
    except OllamaUnavailable as error:
        pytest.skip(f"{client.model} will not answer: {error}")
    return client


#: A function whose complexity is concentrated in one obvious place: the innermost `if`,
#: three levels deep, contributes +4 of the total.
TANGLED = """
def process(orders, customer, region):
    total = 0
    for order in orders:
        if order.active:
            for line in order.lines:
                if line.quantity > 0 and line.price > 0:
                    if region == "eu":
                        total += line.price * 1.2
                    else:
                        total += line.price
    return total
""".lstrip()


def score_and_trail() -> tuple[int, list[str]]:
    profile = get_profile("python")
    data = TANGLED.encode()
    tree = get_parser("python").parse(data)
    parsed = build_file("orders.py", data, profile, tree.root_node)
    function = next(
        node for node in tree.root_node.named_children if node.type in profile.function_like
    )
    result = cognitive_complexity(function, profile, function_name="process")
    del parsed
    return result.score, result.explain()


def test_the_explanation_trail_is_specific_enough_to_act_on(model: OllamaClient) -> None:
    """The product claim: a trail names the line to change, a bare score does not.

    A model given only the number cannot say where to start; given the trail it can. This is
    what `oxn metrics --explain` exists to produce, so it is worth checking rather than
    assuming.
    """
    score, trail = score_and_trail()
    assert score > 12, "the fixture should exceed the configured ceiling"

    prompt = (
        "A static analysis tool reports this Python function has cognitive complexity "
        f"{score}, above the ceiling of 12. Here is its breakdown:\n\n"
        + "\n".join(trail)
        + f"\n\nCode:\n{TANGLED}\n"
        "Which single line number contributes the most to the score? "
        "Answer with just that line number and nothing else."
    )
    answer = model.generate(prompt)

    digits = "".join(char if char.isdigit() else " " for char in answer).split()
    assert digits, f"expected a line number, got {answer!r}"
    # The heaviest increment is the deepest `if`; the trail states its line explicitly.
    heaviest = max(trail, key=lambda entry: int(entry.split()[0].lstrip("+")))
    expected_line = heaviest.split("line ")[1].split(":")[0]
    assert expected_line in digits, (
        f"model answered {digits} but the trail names line {expected_line}: {heaviest}"
    )


def test_the_trail_carries_information_the_score_does_not() -> None:
    """The deterministic half of the claim, which needs no model at all.

    A bare score is one number; the trail names every contributing line. Asserting that
    directly is stronger than asking a model for an opinion about it -- an opinion question
    gets an opinion answer, and reasonable models disagree about "is this actionable".
    """
    score, trail = score_and_trail()
    assert isinstance(score, int)
    lines = {entry.split("line ")[1].split(":")[0] for entry in trail}
    assert len(lines) >= 4, "the trail should name several distinct lines"
    assert all(entry.startswith("+") for entry in trail), "each entry states its own weight"


def test_a_model_can_act_on_the_trail_but_not_on_the_score(model: OllamaClient) -> None:
    """The behavioural difference, tested as capability rather than opinion.

    Given only a number the model has nothing to point at; given the trail it points at a
    line the trail actually names. That gap is the reason `--explain` exists.
    """
    score, trail = score_and_trail()

    with_trail = model.generate(
        "A static analysis tool produced this cognitive-complexity breakdown:\n\n"
        + "\n".join(trail)
        + "\n\nWhich line number should be refactored first? "
        "Answer with just the number."
    )
    named = {token for token in _numbers(with_trail)}
    trail_lines = {entry.split("line ")[1].split(":")[0] for entry in trail}
    assert named & trail_lines, (
        f"with the trail the model should name a line it lists; got {with_trail!r}"
    )

    without_trail = model.generate(
        f"A Python function has a cognitive complexity of {score}. You cannot see the code "
        "and have no other information. Which line number should be refactored first? "
        "If this is impossible to determine, answer exactly UNKNOWN."
    )
    assert "UNKNOWN" in without_trail.upper(), (
        f"without the trail there is nothing to point at; got {without_trail!r}"
    )


def _numbers(text: str) -> list[str]:
    return "".join(char if char.isdigit() else " " for char in text).split()


def test_the_model_agrees_the_nesting_is_the_problem(model: OllamaClient) -> None:
    """A sanity check on the metric's premise: nesting is what makes code hard to read."""
    _, trail = score_and_trail()
    answer = model.generate(
        "Here is a cognitive-complexity breakdown of one function:\n\n"
        + "\n".join(trail)
        + "\n\nIn one word, what is the dominant cause of this score: "
        "nesting, length, or naming?"
    )
    assert "nest" in answer.lower(), f"expected nesting, got {answer!r}"


# ---- the `Writer` seam, with two models through it -------------------------------------------


REVIEW_PAYLOAD = {
    "status": "FINDINGS",
    "base": "origin/main",
    "head": "HEAD",
    "files": {"changed": 12, "measured": 9, "excluded": 3, "errored": 0},
    "counts": {"new": 2, "regression": 1, "baselined": 1, "introduced": 2},
    "findings": [
        {
            "rule": "cognitive_complexity",
            "path": "src/a.py",
            "line": 44,
            "value": 16.0,
            "ceiling": 12.0,
            "origin": "new",
            "introduced": True,
            "was": None,
            "message": "parse has cognitive_complexity 16, above the ceiling of 12",
        },
        {
            "rule": "weighted_methods_per_class",
            "path": "src/c.py",
            "line": 3,
            "value": 31.0,
            "ceiling": 25.0,
            "origin": "baselined",
            "introduced": False,
            "was": 31.0,
            "message": "Store has weighted_methods_per_class 31, above the ceiling of 25",
        },
    ],
    "measured": {
        "commit": "abc1234" + "d" * 33,
        "short": "abc1234",
        "oxn_version": "0.0.1",
        "profiles": {},
        "ceilings": {},
    },
    "errors": {},
}


@pytest.mark.parametrize("name", ["kimi-k3:cloud", "glm-5.3:cloud"])
def test_a_second_model_goes_through_the_same_writer_seam(model, name: str) -> None:
    """**The seam claimed to be pluggable, and nothing had ever plugged anything into it.**

    The roadmap named Anthropic, OpenAI and Gemini and none were built, on the honest grounds
    that a client which has never made a request is not evidence. But that left `Writer` as an
    assertion: one backend, one model, one shape of reply. Ollama reaches hosted models by
    name, so a second *model* exercises the same seam -- and does it in a lane that can run.

    `glm-5.3:cloud` is deliberately the second one. This repository measured it unusable for
    writing code, where it fills any output budget and cuts off mid-answer; restating a JSON
    measurement in four sentences is a different task, and a model is only unusable *at
    something*. Measured 2026-09-14 at four runs each: both first-try on 4 of 4, kimi 479-515
    characters in 15-27 s, glm 444-506 in 8-11 s, neither inventing a number.
    """
    from oxn.review import quotable_numbers, unquotable
    from oxn.writers import ollama_writer, write_review

    comment = write_review(REVIEW_PAYLOAD, ollama_writer(name))

    assert not comment.refused, comment
    assert comment.model == name
    assert unquotable(comment.body, quotable_numbers(REVIEW_PAYLOAD)) == [], comment.body
    assert "src/a.py" in comment.body, "a comment that names no file is not a review"


def test_a_model_that_does_not_exist_costs_the_prose_and_not_the_review() -> None:
    """A typo in `--model` reaches a real host that has never heard of it.

    The same rule as an unreachable host: the measurement was already made, so the review is
    posted with OXN's own body and the reason recorded. Run against the live endpoint because
    "Ollama rejects an unknown model" is a claim about Ollama, and a fake cannot make it.
    """
    from oxn.render import TO_JSON
    from oxn.report import run_review_report

    payload = dict(REVIEW_PAYLOAD)
    import oxn.review as review

    original = review.run_review
    review.run_review = lambda *a, **k: payload
    try:
        comment = run_review_report(
            "main", "HEAD", TO_JSON, write="ollama", model="no-such-model:cloud"
        )["comment"]
    finally:
        review.run_review = original

    assert comment["status"] == "REFUSED"
    assert "**OXN**" in comment["body"], "the review survives a writer that cannot answer"
