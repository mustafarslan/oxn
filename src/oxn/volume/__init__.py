"""Tier 1.5: volume, duplication and erosion.

Syntax-local and exact like Tier 1, but repo-scoped. This tier exists because it measures
precisely what LLM agents degrade: code volume correlates with architectural decay at
rho = 0.94 while prompt specificity does not correlate at all (arXiv 2605.02741), and agent
code is 2.2x more verbose than comparable human projects with erosion rising in 80% of
trajectories (SlopCodeBench, arXiv 2603.24755).
"""

from oxn.volume.clones import Clone, CloneClass, DuplicationReport, find_clones
from oxn.volume.erosion import ErosionReport, gini, structural_erosion
from oxn.volume.tokens import Token, normalized_tokens

__all__ = [
    "Clone",
    "CloneClass",
    "DuplicationReport",
    "ErosionReport",
    "Token",
    "find_clones",
    "gini",
    "normalized_tokens",
    "structural_erosion",
]
