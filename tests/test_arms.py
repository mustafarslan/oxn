"""The six experimental arms, and that they are actually six.

P11's arms are `{no OXN, CLAUDE.md-only, MCP-only, hooks-only, hybrid, hybrid + constraint
budgeting}`, and they are not six prompts -- they are three independent channels, each
matching one way OXN reaches an agent:

* **guidance** -- the rules `oxn init` writes into `CLAUDE.md`;
* **context** -- the increment trail `explain_violation` returns over MCP;
* **enforcement** -- the `PostToolUse` hook rejecting an edit and the agent answering it.

An arm table is only worth running if the arms differ in the way they claim to, so that is
what these check: what each one puts in front of the actor, and how many answers it gets.

The failure this guards against is subtle and fatal to a result. If a no-enforcement arm were
given retries anyway it would re-roll the dice, and its score would credit advice with what
was really just more samples -- an arm beating the control for a reason that has nothing to
do with the thing being tested.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tests.test_dogfood import load_harness


@pytest.fixture(scope="module")
def harness():
    return load_harness()


def _prompt(harness, arm) -> str:
    """The prompt one arm produces for a fixed target, with no feedback yet."""
    target = SimpleNamespace(
        leaf="walk", score=21.0, trail=[f"line {i}: +1 nesting" for i in range(1, 9)]
    )
    ask = harness.Ask(target=target, ceiling=12, file_source="pass", arm=arm)
    return harness.ACTOR_PROMPT.format(
        ceiling=12,
        score=21,
        context=harness._context_block(ask),
        guidance=harness._guidance_block(ask),
        file_source="pass",
        name="walk",
        language="python",
        feedback="",
    )


#: (arm, rules shown, breakdown lines shown). The table in `scripts/arms.py`, as assertions.
EXPECTED = (
    ("none", False, 0),
    ("claude-md", True, 0),
    ("mcp", False, 8),
    ("hooks", False, 0),
    ("hybrid", True, 8),
    ("hybrid-budget", True, 5),
)


@pytest.mark.parametrize(("name", "rules", "trail_lines"), EXPECTED)
def test_each_arm_opens_exactly_the_channels_it_names(
    harness, name: str, rules: bool, trail_lines: int
) -> None:
    prompt = _prompt(harness, harness.arm(name))
    assert ("Rules you must follow" in prompt) is rules, f"{name}: guidance"
    assert prompt.count("+1 nesting") == trail_lines, f"{name}: context"


def test_the_control_arm_is_a_control(harness) -> None:
    """`none` must carry no OXN at all, or the baseline is not a baseline.

    Everything the tool knows -- the rules, the breakdown, the retries -- is what the other
    arms are being measured against. A control that leaked any of it would understate every
    one of them.
    """
    prompt = _prompt(harness, harness.arm("none"))
    assert "Rules you must follow" not in prompt
    assert "breakdown of where the score comes from" not in prompt
    assert not harness.arm("none").enforcement


def test_no_two_arms_are_the_same_experiment(harness) -> None:
    """Six names must be six configurations, or two rows of the table are one row twice."""
    seen = {(a.guidance, a.context, a.enforcement, a.budget) for a in harness.ARMS.values()}
    assert len(seen) == len(harness.ARMS)


def test_an_arm_without_enforcement_gets_one_attempt(harness) -> None:
    """Retries without the gate's rejection are re-rolls, not repairs.

    This is the one that would quietly invalidate a result: an arm scoring better because it
    answered five times, reported as an arm scoring better because it was advised.
    """
    session = harness.Session(ceiling=12, retries=5, arm="claude-md")
    assert harness._attempts_for(session) == 1

    session = harness.Session(ceiling=12, retries=5, arm="hooks")
    assert harness._attempts_for(session) == 5


def test_an_unknown_arm_is_refused_rather_than_defaulted(harness) -> None:
    """Defaulting to `hybrid` would file the control's numbers under the treatment's."""
    with pytest.raises(SystemExit) as raised:
        harness.arm("hybird")
    assert "hybird" in str(raised.value) and "hybrid" in str(raised.value)


def test_budgeting_caps_the_breakdown_without_closing_the_channel(harness) -> None:
    """Constraint budgeting is the hypothesis: fewer constraints, not none.

    An arm that showed nothing would be `claude-md` with extra steps, and the negative result
    the build plan calls publishable -- "budgeting does not mitigate constraint decay" -- needs the
    channel open to mean anything.
    """
    budgeted = harness.arm("hybrid-budget")
    assert budgeted.context and budgeted.budget == harness.DEFAULT_BUDGET
    assert budgeted.trail([f"l{i}" for i in range(20)]) == [f"l{i}" for i in range(5)]
    assert harness.arm("hybrid").trail([f"l{i}" for i in range(20)]) == [f"l{i}" for i in range(20)]


def test_the_prompt_speaks_the_bed_s_language() -> None:
    """It said "Python" for every bed, and the harness has six.

    A Go repair was asked for "one complete Python function definition", with the Go source
    inside a ```python fence. The bed set exists so a result is not measured only on Python
    -- `tests/test_beds.py` asserts one runnable bed per supported language -- and the prompt
    was quietly undoing that for five of them.
    """
    import actor as actor_module
    from actor import Ask, ask_actor
    from targets import Target

    seen: dict[str, str] = {}

    class Recorder:
        def generate(self, prompt: str, system: str = "", **_: object) -> str:
            seen["prompt"], seen["system"] = prompt, system
            return "func Route(a int) int { return a }"

    ask = Ask(
        target=Target(qualified_name="pkg.Route", path="pkg/x.go", score=20.0),
        ceiling=12,
        file_source="package pkg\n",
    )
    ask_actor(Recorder(), ask)

    assert "refactoring go" in seen["system"].lower(), seen["system"]
    assert "python" not in seen["system"].lower()
    assert "```go" in seen["prompt"]
    assert "```python" not in seen["prompt"]
    assert actor_module._language_of("src/lib.rs") == "rust"
