"""Compute every Tier-1 metric for the entities of one file.

The engine is the seam between the parse layer and everything above it: it walks a file
once, matches tree-sitter nodes to the :class:`~oxn.graph.model.Entity` rows the builder
produced, and attaches metric values carrying their own exactness and provenance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from oxn.graph.model import EntityKind, EntityMetrics, MetricValue
from oxn.metrics.cognitive import cognitive_complexity
from oxn.metrics.cyclomatic import cyclomatic_complexity
from oxn.metrics.halstead import halstead, maintainability_index
from oxn.metrics.size import exit_points, line_counts, max_nesting_depth

if TYPE_CHECKING:  # pragma: no cover
    from tree_sitter import Node

    from oxn.graph.model import Entity
    from oxn.profiles.base import LanguageProfile

#: Entities that carry function-shaped metrics.
CALLABLE_KINDS = frozenset({EntityKind.FUNCTION, EntityKind.METHOD, EntityKind.LAMBDA})


def measure_file(
    entities: list[Entity], source: bytes, profile: LanguageProfile, tree_root: Node
) -> list[EntityMetrics]:
    """Measure every entity in a file.

    Entities are matched to nodes by byte range, which is exact: the builder recorded the
    ranges from the same tree.
    """
    by_range = _index_nodes(tree_root, profile)
    results: list[EntityMetrics] = []

    for entity in entities:
        node = by_range.get((entity.start_byte, entity.end_byte)) or by_range.get(
            _inner_range(entity, by_range)
        )
        if node is None and entity.kind is not EntityKind.MODULE:
            continue
        target = node if node is not None else tree_root
        measured = EntityMetrics(entity.id, entity.qualified_name, entity.kind, entity.start_line)
        _measure_common(measured, target, source, profile)
        if entity.kind in CALLABLE_KINDS:
            _measure_callable(measured, target, entity, profile)
        results.append(measured)

    return results


def _measure_common(
    measured: EntityMetrics, node: Node, source: bytes, profile: LanguageProfile
) -> None:
    counts = line_counts(node, source, profile)
    measured.add(MetricValue("lines", counts.lines))
    measured.add(MetricValue("sloc", counts.sloc))
    measured.add(MetricValue("cloc", counts.cloc))
    measured.add(MetricValue("blank", counts.blank))
    measured.add(MetricValue("lloc", counts.lloc))
    measured.add(MetricValue("comment_density", round(counts.comment_density, 4)))

    measures = halstead(node, profile)
    measured.add(MetricValue("halstead_volume", round(measures.volume, 2)))
    measured.add(MetricValue("halstead_difficulty", round(measures.difficulty, 2)))
    measured.add(MetricValue("halstead_effort", round(measures.effort, 2)))
    measured.add(MetricValue("halstead_vocabulary", measures.vocabulary))
    measured.add(MetricValue("halstead_length", measures.length))


def _measure_callable(
    measured: EntityMetrics, node: Node, entity: Entity, profile: LanguageProfile
) -> None:
    cyclomatic = cyclomatic_complexity(node, profile)
    measured.add(MetricValue("cyclomatic_complexity", cyclomatic))

    cognitive = cognitive_complexity(node, profile, function_name=entity.name)
    # The specification increments for "each method in a recursion cycle, whether direct or
    # indirect". Without a resolved call graph only *direct* self-recursion is visible, so
    # a function that calls anything might be understated -- but never overstated, which is
    # what makes the value a sound lower bound for a ceiling gate.
    makes_calls = _contains_call(node, profile)
    measured.add(
        MetricValue(
            "cognitive_complexity",
            cognitive.score,
            exactness="APPROX" if makes_calls else "EXACT",
            explanation=tuple(cognitive.explain()),
            bound="lower" if makes_calls else "exact",
        )
    )
    measured.add(MetricValue("max_nesting_depth", max_nesting_depth(node, profile)))
    measured.add(MetricValue("exit_points", exit_points(node, profile)))
    measured.add(MetricValue("parameter_count", len(entity.attrs.get("parameters", []))))

    volume = measured.get("halstead_volume") or 0.0
    sloc = int(measured.get("sloc") or 0)
    density = measured.get("comment_density") or 0.0
    index = maintainability_index(volume, cyclomatic, sloc, density)
    # Reported for compatibility, never gated on -- see docs/metrics.md section 3.7.
    measured.add(MetricValue("maintainability_index", round(index.visual_studio, 2)))


def _contains_call(node: Node, profile: LanguageProfile) -> bool:
    """True when a function calls anything, so indirect recursion cannot be ruled out."""
    call_kinds = profile.metrics.cognitive.call_kinds
    if not call_kinds:
        return False
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in call_kinds:
            return True
        stack.extend(current.named_children)
    return False


def _index_nodes(root: Node, profile: LanguageProfile) -> dict[tuple[int, int], Node]:
    """Byte range -> node, for every node an entity could have been built from."""
    index: dict[tuple[int, int], Node] = {(root.start_byte, root.end_byte): root}
    stack = [root]
    while stack:
        current = stack.pop()
        for child in current.named_children:
            definition = profile.unwrap(child)
            if profile.is_definition(definition):
                # Record both spans: the builder uses the wrapper's range for the entity,
                # while the metrics must run over the definition itself.
                index[(child.start_byte, child.end_byte)] = definition
                index[(definition.start_byte, definition.end_byte)] = definition
            stack.append(child)
    return index


def _inner_range(entity: Entity, index: dict[tuple[int, int], Node]) -> tuple[int, int]:
    return (entity.start_byte, entity.end_byte)
