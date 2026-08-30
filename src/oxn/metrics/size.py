"""Size and shape metrics: lines, statements, nesting depth, parameters, exit points.

Counting rules follow SEI's framework for counting source statements (Park,
*Software Size Measurement*, CMU/SEI-92-TR-020). Lines are derived from the **token
stream's line spans, not regular expressions**, which is what makes multi-line strings,
JSX, raw strings and heredocs come out right.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from tree_sitter import Node

    from oxn.profiles.base import LanguageProfile


@dataclass(frozen=True, slots=True)
class LineCounts:
    """Physical, logical, comment and blank line counts for a span of source."""

    lines: int
    sloc: int
    cloc: int
    blank: int
    lloc: int

    @property
    def comment_density(self) -> float:
        denominator = self.sloc + self.cloc
        return self.cloc / denominator if denominator else 0.0


def line_counts(node: Node, source: bytes, profile: LanguageProfile) -> LineCounts:
    """Count lines over ``node``'s span.

    A line holding both code and a trailing comment counts once in each of ``sloc`` and
    ``cloc``, which is why they can sum to more than ``lines``.
    """
    spec = profile.metrics.size
    first, last = node.start_point[0], node.end_point[0]
    total = last - first + 1

    comment_lines: set[int] = set()
    code_lines: set[int] = set()
    lloc = 0

    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in spec.statement_kinds:
            lloc += 1
        is_comment = current.type in profile.comment_kinds or (
            spec.docstrings_are_comments and _is_docstring(current, profile)
        )
        if is_comment:
            comment_lines.update(range(current.start_point[0], current.end_point[0] + 1))
            continue
        if current.child_count == 0 and current.type not in profile.comment_kinds:
            code_lines.update(range(current.start_point[0], current.end_point[0] + 1))
        stack.extend(current.children)

    blank_or_missing = set(range(first, last + 1)) - code_lines - comment_lines
    blank = len(_non_empty_only(blank_or_missing, source, first, last))

    return LineCounts(
        lines=total,
        sloc=len(code_lines),
        cloc=len(comment_lines),
        blank=blank,
        lloc=lloc,
    )


def _non_empty_only(candidates: set[int], source: bytes, first: int, last: int) -> set[int]:
    """Of the lines with no tokens, keep those that really are blank in the source."""
    lines = source.split(b"\n")
    return {i for i in candidates if first <= i <= last and not lines[i].strip()}


def _is_docstring(node: Node, profile: LanguageProfile) -> bool:
    """A bare string expression in statement position -- documentation, not data."""
    if node.type not in profile.string_kinds:
        return False
    parent = node.parent
    return parent is not None and parent.type == "expression_statement"


def max_nesting_depth(node: Node, profile: LanguageProfile) -> int:
    """Deepest block nesting inside a function.

    Reuses the cognitive-complexity nesting set, so "nested 3 deep" means the same thing in
    a complexity explanation and in this metric.
    """
    spec = profile.metrics.cognitive
    nesting_kinds = spec.structural | spec.hybrid

    def walk(current: Node, depth: int) -> int:
        deepest = depth
        for child in current.named_children:
            target = profile.unwrap(child)
            if profile.is_definition(target) and target is not node:
                continue  # nested definitions own their own depth
            next_depth = depth + 1 if target.type in nesting_kinds else depth
            deepest = max(deepest, walk(target, next_depth))
        return deepest

    return walk(node, 0)


def exit_points(node: Node, profile: LanguageProfile) -> int:
    """Number of ``return``/``raise``-style exits, excluding nested definitions."""
    spec = profile.metrics.size
    count = 0
    stack = list(node.named_children)
    while stack:
        current = stack.pop()
        target = profile.unwrap(current)
        if profile.is_definition(target):
            continue
        if target.type in spec.return_kinds:
            count += 1
        stack.extend(target.named_children)
    return count
