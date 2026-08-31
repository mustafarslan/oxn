"""The normalized token stream, shared by Halstead and clone detection.

Both need the same thing -- the leaf tokens of a subtree, classified into operators and
operands -- so they read one implementation. Forking the two would let a classification fix
land in one metric and not the other, and the profile's Halstead tables are the single
source of that classification (docs/metrics.md sections 3.6 and 3A.1).

**Normalization is what turns Type-1 clone detection into Type-2.** Every identifier
becomes ``$ID`` and every literal ``$LIT``, so a copied block survives renaming. Operators
and keywords are kept verbatim, because they are the structure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator

    from tree_sitter import Node

    from oxn.profiles.base import LanguageProfile

#: Stand-ins that make renamed code compare equal.
IDENTIFIER_PLACEHOLDER = "$ID"
LITERAL_PLACEHOLDER = "$LIT"

_LITERAL_HINTS = ("string", "number", "integer", "float", "char", "regex", "template")


@dataclass(frozen=True, slots=True)
class Token:
    """One leaf token, with everything both consumers need."""

    text: str
    kind: str
    is_operand: bool
    start_byte: int
    end_byte: int
    line: int

    @property
    def normalized(self) -> str:
        """Text with identifiers and literals erased, for clone comparison."""
        if not self.is_operand:
            return self.text
        return LITERAL_PLACEHOLDER if _is_literal(self.kind) else IDENTIFIER_PLACEHOLDER


def _is_literal(kind: str) -> bool:
    return any(hint in kind for hint in _LITERAL_HINTS) or kind in {
        "true",
        "false",
        "none",
        "null",
        "undefined",
        "ellipsis",
    }


def normalized_tokens(node: Node, profile: LanguageProfile) -> list[Token]:
    """Leaf tokens of ``node`` in source order, classified and normalizable.

    Comments are dropped. A closing delimiter is dropped too, matching Halstead's
    convention that a matched pair is one operator -- and harmlessly for clone detection,
    since the opening delimiter already marks the structure.
    """
    spec = profile.metrics.halstead
    return [
        Token(
            text=text,
            kind=leaf.type,
            is_operand=leaf.type in spec.operand_kinds,
            start_byte=leaf.start_byte,
            end_byte=leaf.end_byte,
            line=leaf.start_point[0] + 1,
        )
        for leaf, text in iter_leaves(node, profile)
    ]


def iter_leaves(node: Node, profile: LanguageProfile) -> Iterator[tuple[Node, str]]:
    """Every leaf token that counts, with its text, in source order.

    Shared with `oxn.metrics.halstead`, which asks the same question for a different
    purpose. The rule for *which* leaves count -- comments dropped, excluded subtrees
    pruned rather than walked into, a closing delimiter dropped because a matched pair is
    one operator -- has to be the same in both, or a token stream and its Halstead
    classification would disagree about what the file contains.

    Iterative: a deeply nested expression is enough to reach Python's recursion limit, and
    the crash says nothing about which file did it.
    """
    spec = profile.metrics.halstead
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in spec.excluded_kinds or current.type in profile.comment_kinds:
            continue
        if current.child_count:
            stack.extend(reversed(current.children))
            continue
        text = current.text.decode("utf-8", "replace") if current.text else ""
        if current.type in spec.close_delimiters or text in spec.excluded_tokens:
            continue
        yield current, text
