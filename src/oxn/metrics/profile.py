"""Risk profiles: how much of a codebase sits in code that is over budget, and which code.

`docs/metrics.md` section 10.1 asks for two outputs and forbids conflating them. The gate is
boolean, scoped to changed entities, and always explainable. This is the other one -- the
health view, project- and component-level, which never blocks anything.

**Why a profile and not a rating.** Section 10.2's recipe ends by mapping a profile to a 1-5
rating "via calibrated boundaries", and SIG derived those from roughly a hundred systems. OXN
has five, and they are the same five its ceilings were measured against, so a rating fitted to
them would make httpx's health score a statement about how httpx compares to httpx. P10's exit
criterion asks for a score that is "stable and explainable -- every point traces back to named
entities", and a profile is exactly that: *"18% of callable lines sit in functions over the
cognitive ceiling, and here they are."* A star rating traces to a boundary table instead. The
rating and the composite of section 10.4 wait for a corpus large enough to calibrate them.

**Why the buckets are the project's own ceilings.** The alternative was corpus percentiles per
language, which would have invented fifteen new tunables from one repository each. A declared
ceiling is already argued, already recorded in `oxn.calibration`, and is the same number the
gate uses -- so "over budget" means one thing in both outputs rather than two.

Weighting is by SLOC, not by entity count, for the reason section 10.2 gives: these
distributions are heavy-tailed, and one 300-line function is not one three-line function.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable


@dataclass(frozen=True, slots=True)
class Offender:
    """One entity carrying part of a profile's over-budget share."""

    name: str
    path: str
    line: int
    value: float
    sloc: float

    def as_dict(self) -> dict[str, object]:
        return {
            "entity": self.name,
            "path": self.path,
            "line": self.line,
            "value": self.value,
            "sloc": self.sloc,
        }


@dataclass
class Profile:
    """One metric's risk profile: the share over budget, and who carries it."""

    metric: str
    ceiling: float
    #: Total source lines in the entities this metric governs.
    total_sloc: float = 0.0
    #: Source lines in entities over the ceiling.
    over_sloc: float = 0.0
    counted: int = 0
    over: int = 0
    offenders: list[Offender] = field(default_factory=list)

    @property
    def share(self) -> float:
        """Fraction of governed lines that sit over the ceiling."""
        return self.over_sloc / self.total_sloc if self.total_sloc else 0.0

    def as_dict(self, limit: int = 10) -> dict[str, object]:
        """The profile, with the entities that account for it.

        `offenders` is the whole point: a share with no names attached is the number section
        10.1 refuses, and no amount of precision makes it actionable.
        """
        worst = sorted(self.offenders, key=lambda found: -found.value)[:limit]
        return {
            "metric": self.metric,
            "ceiling": self.ceiling,
            "share_over_ceiling": round(self.share, 6),
            "entities": self.counted,
            "entities_over": self.over,
            "sloc": self.total_sloc,
            "sloc_over": self.over_sloc,
            "worst": [found.as_dict() for found in worst],
        }


@dataclass(frozen=True, slots=True)
class Measured:
    """One entity as a profile sees it, whatever produced the numbers."""

    name: str
    path: str
    line: int
    sloc: float
    value: float


def profile(metric: str, ceiling: float, entities: Iterable[Measured]) -> Profile:
    """Weigh one metric's entities against one ceiling."""
    found = Profile(metric=metric, ceiling=ceiling)
    for entity in entities:
        weight = max(entity.sloc, 1.0)
        found.total_sloc += weight
        found.counted += 1
        if entity.value > ceiling:
            found.over_sloc += weight
            found.over += 1
            found.offenders.append(
                Offender(entity.name, entity.path, entity.line, entity.value, weight)
            )
    return found
