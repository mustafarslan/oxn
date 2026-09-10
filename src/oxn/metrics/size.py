"""Size and shape metrics: lines, statements, nesting depth, parameters, exit points.

Counting rules follow SEI's framework for counting source statements (Park,
*Software Size Measurement*, CMU/SEI-92-TR-020). Lines are derived from the **token
stream's line spans, not regular expressions**, which is what makes multi-line strings,
JSX, raw strings and heredocs come out right.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from oxn.metrics.cognitive import else_if_inner

if TYPE_CHECKING:  # pragma: no cover
    from tree_sitter import Node

    from oxn.profiles.base import LanguageProfile
    from oxn.profiles.spec import CognitiveSpec, SizeSpec


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
        if current.type in spec.statement_kinds and not _wrapped_statement(current, spec):
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


def _wrapped_statement(node: Node, spec: SizeSpec) -> bool:
    """True when this statement is the whole of another statement, and so is not a second one.

    Rust's `return a;` is a `return_expression` inside an `expression_statement`, and both
    kinds are counted, so a function whose entire body was one `return` reported three
    logical lines against Go's and Python's two. Requiring it to be the parent's *sole* named
    child is what keeps this from swallowing a real statement: a block holding three of them
    is three, and only a wrapper that adds nothing but syntax is dropped.
    """
    parent = node.parent
    if parent is None or parent.type not in spec.statement_kinds:
        return False
    return len(parent.named_children) == 1


def _non_empty_only(candidates: set[int], source: bytes, first: int, last: int) -> set[int]:
    """Of the lines with no tokens, keep those that really are blank in the source."""
    lines = source.split(b"\n")
    return {i for i in candidates if first <= i <= last and not lines[i].strip()}


#: Node kinds whose direct children are statements. A string sitting immediately under one
#: of these is a bare string expression -- a docstring -- rather than a value being used.
_STATEMENT_POSITION = frozenset({"expression_statement", "block", "module"})


def _is_docstring(node: Node, profile: LanguageProfile) -> bool:
    """A bare string expression in statement position -- documentation, not data.

    **Requiring an `expression_statement` parent meant this never fired.** The Python
    grammar OXN ships emits a docstring as a `string` directly under its `block` -- there is
    no wrapper -- so `docstrings_are_comments` was set, read, and could not match. Every
    Python docstring counted as code: `sloc` on an eight-line function with a six-line
    docstring read 8, `cloc` read 0, and `comment_density` read 0.0 on files that are more
    prose than statements. `MAX_FUNCTION_SLOC` and `MAX_FILE_SLOC` gate on that number, so
    the gate had been charging this repository -- whose house style is long explanatory
    docstrings -- for writing them.

    `block` and `module` are kept alongside `expression_statement` rather than replacing it,
    because which of the three a grammar emits is a grammar-version detail and a metric must
    not depend on one. What all three share is the property the docstring names: the string
    stands where a *statement* stands. A string under `assignment`, `pair` or `argument_list`
    is a value, and none of those is in the set.
    """
    if node.type not in profile.string_kinds:
        return False
    parent = node.parent
    return parent is not None and parent.type in _STATEMENT_POSITION


def max_nesting_depth(node: Node, profile: LanguageProfile) -> int:
    """Deepest block nesting inside a function.

    Reuses the cognitive-complexity nesting *rule*, so "nested 3 deep" means the same thing
    in a complexity explanation and in this metric. Reusing only its set of nesting kinds was
    not enough, and the gap was measurable: one `else if` chain transliterated into six
    languages read depth 4 in TypeScript and Rust against 3 in Python, Java and Go, because
    the two grammars that wrap `else if` in an `else_clause` had the wrapper *and* the `if`
    it holds counted as separate levels. A gated ceiling that answers differently for the
    same logic is a ceiling that means nothing across a polyglot repository.

    An `else` branch continues its `if` rather than nesting under it -- Sonar's rule, and the
    one `_visit_alternative` already applies -- so the branches named by
    `same_nesting_fields`, and the `if` inside a wrapped `else_clause`, add no level of their
    own. Their *bodies* still do, which is what keeps a genuinely nested `if` at depth 2.
    """
    spec = profile.metrics.cognitive
    nesting_kinds = spec.structural | spec.hybrid

    def walk(current: Node, depth: int, continues: frozenset[int]) -> int:
        deepest = depth
        for child in current.named_children:
            target = profile.unwrap(child)
            if profile.is_definition(target) and target is not node:
                continue  # nested definitions own their own depth
            deeper = target.type in nesting_kinds and target.id not in continues
            deepest = max(deepest, walk(target, depth + deeper, _continuations(target, spec)))
        return deepest

    return walk(node, 0, frozenset())


def _continuations(node: Node, spec: CognitiveSpec) -> frozenset[int]:
    """Children that continue this conditional's `else` chain rather than nesting under it.

    Two grammar shapes, one meaning. A wrapped `else if` hands back the `if` inside its
    `else_clause`; a direct one hands back whatever sits in the `alternative` slot, which is
    the next `if` in Go and Java and a bare block for a plain `else`.
    """
    inner = else_if_inner(node, spec)
    if inner is not None:
        return frozenset({inner.id})
    return frozenset(
        child.id
        for field_name in spec.same_nesting_fields
        for child in node.children_by_field_name(field_name)
    )


def exit_points(node: Node, profile: LanguageProfile) -> int:
    """Number of ``return``/``raise``-style exits, excluding nested definitions.

    Plus the one exit that is written with no keyword at all: Rust ends a function on its
    body's final expression, and a function leaves through that exactly as another language
    leaves through a `return`. Transliterating one function into six languages read four
    exits in Rust against five in the other five, and the missing one was the `-1` on the
    last line.
    """
    spec = profile.metrics.size
    count = 1 if _has_tail_return(node, profile) else 0
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


def _has_tail_return(node: Node, profile: LanguageProfile) -> bool:
    """True when this callable ends on a bare expression that is its return value.

    The grammar makes it exact rather than a guess: a block's statements are statement nodes,
    so a final child that is *not* one is the tail expression and nothing else can be.
    """
    spec = profile.metrics.size
    body = node.child_by_field_name(profile.body_field)
    if not spec.tail_expression_returns or body is None or not body.named_children:
        return False
    last = body.named_children[-1]
    return last.type not in spec.statement_kinds and last.type != "expression_statement"
