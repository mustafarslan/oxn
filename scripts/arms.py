"""The experimental arms: which of OXN's three channels an actor is given.

P11 names six arms -- `{no OXN, CLAUDE.md-only, MCP-only, hooks-only, hybrid, hybrid +
constraint budgeting}` -- and they are not six prompts. They are three independent channels,
and the arms are the interesting combinations of them:

============  ========  =======  ===========  ==============================================
arm           guidance  context  enforcement  what the actor is given
============  ========  =======  ===========  ==============================================
none          no        no       no           "this is too complex, rewrite it"
claude-md     **yes**   no       no           the rules `oxn init` writes, and nothing else
mcp           no        **yes**  no           the score's breakdown, as `explain_violation`
                                              returns it, and no rules
hooks         no        no       **yes**      no advice at all, and a gate that rejects and
                                              says why, with retries to answer it
hybrid        yes       yes      yes          all three
hybrid-budget yes       yes      yes          all three, with the breakdown capped
============  ========  =======  ===========  ==============================================

The mapping is faithful rather than convenient. `oxn init` writes the rules into `CLAUDE.md`;
`explain_violation` returns the increment trail; the `PostToolUse` hook rejects an edit and
the agent tries again. Each is one channel here, and an arm is which of them are open.

**Enforcement is what makes a retry meaningful.** Without the gate's rejection there is
nothing to retry *against*, so an arm with `enforcement=False` gets a single attempt. Giving
it retries anyway would let it re-roll the dice and score as though feedback had helped --
attributing to advice what was really just more samples.

**Constraint budgeting is the hypothesis, not the control.** Constraint decay is the risk
that more constraints make an agent worse; budgeting is the proposed mitigation, and it caps
how much of the breakdown the actor sees at once. It is the arm that can produce the negative
result the build plan calls publishable, which is why it is separate from `hybrid` rather than a
flag on it.
"""

from __future__ import annotations

from dataclasses import dataclass

#: How many lines of the score's breakdown the budgeted arm may see. Chosen, not fitted --
#: it is the parameter the experiment exists to measure, and `oxn.calibration` is where a
#: fitted value would have to be recorded with its evidence.
DEFAULT_BUDGET = 5


@dataclass(frozen=True, slots=True)
class Arm:
    """One combination of the three channels, and what to call it."""

    name: str
    #: The rules block. `oxn init` writes exactly this kind of text into `CLAUDE.md`.
    guidance: bool
    #: OXN's own account of where the score comes from -- the increment trail
    #: `explain_violation` returns over MCP.
    context: bool
    #: The gate's rejection, fed back, with the retries that make it enforcement rather than
    #: advice. An arm without it gets one attempt; see the module docstring.
    enforcement: bool
    #: Lines of breakdown the actor may see at once. 0 means all of them.
    budget: int = 0

    @property
    def retries(self) -> int:
        """How many repairs this arm may attempt beyond the first."""
        return 0 if not self.enforcement else -1  # -1: the session's own budget applies

    def trail(self, lines: list[str]) -> list[str]:
        """The breakdown this arm shows, which for two arms is none of it."""
        if not self.context:
            return []
        return lines[: self.budget] if self.budget else lines


ARMS: dict[str, Arm] = {
    "none": Arm("none", guidance=False, context=False, enforcement=False),
    "claude-md": Arm("claude-md", guidance=True, context=False, enforcement=False),
    "mcp": Arm("mcp", guidance=False, context=True, enforcement=False),
    "hooks": Arm("hooks", guidance=False, context=False, enforcement=True),
    "hybrid": Arm("hybrid", guidance=True, context=True, enforcement=True),
    "hybrid-budget": Arm(
        "hybrid-budget", guidance=True, context=True, enforcement=True, budget=DEFAULT_BUDGET
    ),
}


def arm(name: str) -> Arm:
    """The arm registered under `name`, refused with the list if there is none.

    Defaulting a typo to `hybrid` would file one arm's numbers under another's, which is the
    single failure that makes an arm table worthless -- the same rule the actor registry
    applies for the same reason.
    """
    found = ARMS.get(name)
    if found is None:
        raise SystemExit(f"unknown arm {name!r}. Available: {', '.join(ARMS)}")
    return found


def arm_names() -> tuple[str, ...]:
    """Every arm, in the order the table above reads."""
    return tuple(ARMS)
