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
    from oxn.profiles.spec import CyclomaticSpec


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
    total = 0
    for child in node.named_children:
        definition = profile.unwrap(child)
        if not is_root and profile.is_definition(definition):
            continue  # a nested definition owns its own score
        total += _decisions_at(definition, profile, decision_points)
        total += _count_decisions(child, profile, decision_points, is_root=False)
    return total


def _decisions_at(
    definition: Node, profile: LanguageProfile, decision_points: frozenset[str]
) -> int:
    """Decisions this node contributes on its own account, its children excluded."""
    spec = profile.metrics.cyclomatic
    if definition.type in spec.exhaustive_multiway_kinds:
        # n exhaustive branches give n paths, so n-1 decisions -- see the spec field. A
        # two-arm Rust `match` is exactly an `if`/`else` and must score the same.
        return max(0, _count_branches(definition, spec) - 1)

    total = 0
    if definition.type in decision_points and not _is_catch_all(definition, spec):
        total += 1
    if definition.type == spec.boolean_node:
        total += _count_boolean_operators(definition, spec.boolean_node, spec.boolean_operators)
    return total


def _count_branches(node: Node, spec: CyclomaticSpec) -> int:
    """Branches belonging to *this* multiway construct.

    The search stops at a nested one: a ``match`` inside a ``match`` arm owns its own
    branches, and counting them twice inflates every nested pattern match.
    """
    total = 0
    stack = list(node.named_children)
    while stack:
        current = stack.pop()
        if current.type in spec.exhaustive_multiway_kinds:
            continue
        if current.type in spec.multiway_branch_kinds:
            total += 1
        stack.extend(current.named_children)
    return total


def _is_catch_all(node: Node, spec: CyclomaticSpec) -> bool:
    """True for a default branch, which is a fall-through rather than a decision.

    Both grammars spell the wildcard as an *anonymous* token, so a catch-all pattern is one
    with no named children -- ``case _:`` and ``_ =>`` alike. Testing for the pattern node's
    kind instead would match every case, which silently zeroes out the metric.
    """
    if node.type not in spec.catch_all_kinds:
        return False
    if node.type in spec.catch_all_pattern_kinds:
        # The branch node *is* its own pattern -- Java's `switch_label`.
        return not node.named_children
    for child in node.named_children:
        if child.type in spec.catch_all_pattern_kinds:
            return not child.named_children
    return False


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
