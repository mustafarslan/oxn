"""Halstead's measures (Halstead, *Elements of Software Science*, Elsevier, 1977) and the
Maintainability Index.

**The classification is the hard part, and there is no standard.** radon, lizard and
rust-code-analysis each disagree about what counts as an operator in Python, let alone
across languages. OXN therefore publishes its own versioned table
(:class:`~oxn.profiles.spec.HalsteadSpec`) and treats third-party tools as *correlation*
oracles, never equality oracles. A value is exact with respect to OXN's table and
approximate as a reproduction of anyone else's.

Cross-language comparability is the reason for owning the table: adopting radon would give
Python numbers that cannot be compared with our Go numbers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from oxn.volume.tokens import iter_leaves

if TYPE_CHECKING:  # pragma: no cover
    from tree_sitter import Node

    from oxn.profiles.base import LanguageProfile


@dataclass(frozen=True, slots=True)
class Halstead:
    """The Halstead suite for one span of code."""

    distinct_operators: int  # n1
    distinct_operands: int  # n2
    total_operators: int  # N1
    total_operands: int  # N2
    spec_version: int

    @property
    def vocabulary(self) -> int:
        return self.distinct_operators + self.distinct_operands

    @property
    def length(self) -> int:
        return self.total_operators + self.total_operands

    @property
    def volume(self) -> float:
        return self.length * math.log2(self.vocabulary) if self.vocabulary else 0.0

    @property
    def difficulty(self) -> float:
        if not self.distinct_operands:
            return 0.0
        return (self.distinct_operators / 2) * (self.total_operands / self.distinct_operands)

    @property
    def effort(self) -> float:
        return self.difficulty * self.volume

    @property
    def time_seconds(self) -> float:
        """Halstead's estimate, effort divided by the Stroud number of 18."""
        return self.effort / 18

    @property
    def estimated_length(self) -> float:
        def term(n: int) -> float:
            return n * math.log2(n) if n else 0.0

        return term(self.distinct_operators) + term(self.distinct_operands)

    @property
    def bugs(self) -> float:
        """Halstead's delivered-bugs estimate. Reported, never gated on."""
        return self.volume / 3000


def halstead(node: Node, profile: LanguageProfile) -> Halstead:
    """Classify the leaf tokens of ``node`` into operators and operands.

    Tree-sitter's named/anonymous distinction does most of the work: anonymous leaves are
    punctuation and keywords (operators), named leaves are identifiers and literals
    (operands). A matched delimiter pair counts as one operator, per Halstead's own
    convention, so the closing token is skipped.
    """
    spec = profile.metrics.halstead
    operators: dict[str, int] = {}
    operands: dict[str, int] = {}

    for leaf, text in iter_leaves(node, profile):
        bucket = operands if leaf.type in spec.operand_kinds else operators
        bucket[text] = bucket.get(text, 0) + 1

    return Halstead(
        distinct_operators=len(operators),
        distinct_operands=len(operands),
        total_operators=sum(operators.values()),
        total_operands=sum(operands.values()),
        spec_version=spec.spec_version,
    )


@dataclass(frozen=True, slots=True)
class MaintainabilityIndex:
    """The three published variants, all reported, none gated on.

    OXN computes MI as a compatibility feature and **never uses it as a gate**. The
    coefficients were regression-fitted to a small 1990s Hewlett-Packard corpus of Fortran
    and Pascal; there is no evidence they transfer to Python, TypeScript, Go or Rust. It
    inherits Halstead's classification ambiguity, Sjoberg et al. (ESEM 2012) found it a poor
    predictor of real maintenance effort, and its comment term rewards comment volume --
    trivially gameable, which is disqualifying when the thing being governed is an agent.

    The SIG-style risk profile is the gate instead. See docs/metrics.md section 3.7.
    """

    original: float
    sei: float
    visual_studio: float


def maintainability_index(
    volume: float, cyclomatic: int, sloc: int, comment_ratio: float
) -> MaintainabilityIndex:
    """Oman & Hagemeister (ICSM 1992); Coleman et al. (*IEEE Computer* 27(8), 1994)."""
    safe_volume = max(volume, 1.0)
    safe_sloc = max(sloc, 1)

    original = 171 - 5.2 * math.log(safe_volume) - 0.23 * cyclomatic - 16.2 * math.log(safe_sloc)
    sei = original + 50 * math.sin(math.sqrt(2.4 * comment_ratio))
    visual_studio = max(0.0, original * 100 / 171)

    return MaintainabilityIndex(
        original=original,
        sei=sei,
        visual_studio=visual_studio,
    )
