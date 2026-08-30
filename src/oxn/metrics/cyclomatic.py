"""Cyclomatic Complexity (McCabe 1976).

``M = E - V + 2P`` over the control-flow graph; for structured programs, equivalently
``M = pi + 1`` where ``pi`` counts decision predicates. OXN uses the decision-point form.

**There is no cross-language standard**, and the free tools disagree with each other on
real Python: radon counts ``assert`` and loop-``else``, lizard counts neither but counts
``finally`` and each ``match`` case. So the per-language table in
:class:`~oxn.profiles.spec.CyclomaticSpec` *is* OXN's definition, and every divergence is
recorded in ``docs/divergences.md`` rather than left for a user to discover.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from tree_sitter import Node

    from oxn.profiles.base import LanguageProfile


def cyclomatic_complexity(node: Node, profile: LanguageProfile) -> int:
    """Complexity of one function body, excluding definitions nested inside it.

    Nested functions are separate entities in the code graph and get their own score, so
    the walk stops at their boundary. That keeps a parent's number about the parent.
    """
    spec = profile.metrics.cyclomatic
    return 1 + _count_decisions(node, profile, spec.decision_points, is_root=True)


def _count_decisions(
    node: Node, profile: LanguageProfile, decision_points: frozenset[str], *, is_root: bool
) -> int:
    spec = profile.metrics.cyclomatic
    total = 0

    for child in node.named_children:
        definition = profile.unwrap(child)
        if not is_root and profile.is_definition(definition):
            continue  # a nested definition owns its own score
        if definition.type in decision_points:
            total += 1
        if definition.type == spec.boolean_node:
            total += _count_boolean_operators(definition, spec.boolean_node, spec.boolean_operators)
        total += _count_decisions(child, profile, decision_points, is_root=False)

    return total


def _count_boolean_operators(
    node: Node, boolean_node: str | None, operators: frozenset[str]
) -> int:
    """One decision point per binary logical operator.

    Only the operator on *this* node; nested operands are reached by the ordinary walk.
    """
    if boolean_node is None:
        return 0
    operator = node.child_by_field_name("operator")
    if operator is None or operator.text is None:
        return 0
    return 1 if operator.text.decode("utf-8", "replace") in operators else 0
