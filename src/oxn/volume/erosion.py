"""Structural erosion: how concentrated a codebase's complexity has become.

SlopCodeBench's second trajectory signal, and the reason it is a *concentration* measure
rather than a mean: software metric distributions are heavy-tailed (Louridas, Spinellis &
Vlachos, "Power laws in software", *TOSEM* 18(1), 2008), so mean complexity barely moves as
a codebase degrades. Concentration moves.

Two forms, both reported:

* ``erosion@k`` -- the share of total complexity held by the worst ``k`` fraction of
  functions. Readable ("62% of your complexity lives in 10% of your functions") and the
  right shape for a single project's trend line, but it depends on function count.
* the **Gini coefficient** of the complexity distribution -- scale-free, so it is the one
  to use when comparing projects.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from oxn.thresholds import EROSION_TOP_FRACTION

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence


@dataclass(frozen=True, slots=True)
class ErosionReport:
    """Concentration of complexity across a set of functions."""

    function_count: int
    total_complexity: float
    top_fraction: float
    erosion_at_k: float
    gini: float
    worst: tuple[tuple[str, float], ...] = ()

    def as_dict(self) -> dict[str, float | int]:
        return {
            "function_count": self.function_count,
            "total_complexity": self.total_complexity,
            f"erosion_at_{int(self.top_fraction * 100)}pct": round(self.erosion_at_k, 4),
            "gini": round(self.gini, 4),
        }


def gini(values: Sequence[float]) -> float:
    """Gini coefficient of a non-negative distribution, in ``[0, 1)``.

    ``0`` means every function carries the same complexity; higher means the mass is
    concentrated in fewer of them. Uses the standard sorted formulation
    ``G = (2 * sum(i * x_i)) / (n * sum(x)) - (n + 1) / n`` with ``i`` one-based.
    """
    ordered = sorted(float(value) for value in values)
    count = len(ordered)
    if count == 0:
        return 0.0
    total = math.fsum(ordered)
    if total <= 0:
        return 0.0
    weighted = math.fsum(index * value for index, value in enumerate(ordered, start=1))
    return (2 * weighted) / (count * total) - (count + 1) / count


def erosion_at(values: Sequence[float], fraction: float = EROSION_TOP_FRACTION) -> float:
    """Share of total complexity held by the worst ``fraction`` of functions.

    The count is rounded up, so a small codebase still reports something meaningful: with
    12 functions and ``fraction=0.1``, the worst 2 are considered rather than zero.
    """
    ordered = sorted((float(value) for value in values), reverse=True)
    if not ordered:
        return 0.0
    total = math.fsum(ordered)
    if total <= 0:
        return 0.0
    count = max(1, math.ceil(len(ordered) * fraction))
    return math.fsum(ordered[:count]) / total


def structural_erosion(
    scores: dict[str, float], *, fraction: float = EROSION_TOP_FRACTION, worst: int = 10
) -> ErosionReport:
    """Build the report from ``qualified name -> complexity``."""
    values = list(scores.values())
    ranked = sorted(scores.items(), key=lambda item: -item[1])[:worst]
    return ErosionReport(
        function_count=len(values),
        total_complexity=math.fsum(values),
        top_fraction=fraction,
        erosion_at_k=erosion_at(values, fraction),
        gini=gini(values),
        worst=tuple(ranked),
    )
