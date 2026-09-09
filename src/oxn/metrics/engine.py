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
from oxn.metrics.shredding import METRIC, Cluster, Unit, cluster_totals
from oxn.metrics.size import exit_points, line_counts, max_nesting_depth

if TYPE_CHECKING:  # pragma: no cover
    from tree_sitter import Node

    from oxn.graph.model import Entity
    from oxn.profiles.base import LanguageProfile

#: Entities that carry function-shaped metrics.
CALLABLE_KINDS = frozenset({EntityKind.FUNCTION, EntityKind.METHOD, EntityKind.LAMBDA})
#: Entities that carry the class aggregates.
CLASS_KINDS = frozenset({EntityKind.CLASS, EntityKind.INTERFACE})


def measure_file(
    entities: list[Entity], source: bytes, profile: LanguageProfile, tree_root: Node
) -> list[EntityMetrics]:
    """Measure every entity in a file.

    Entities are matched to nodes by byte range, which is exact: the builder recorded the
    ranges from the same tree.
    """
    by_range = _index_nodes(tree_root, profile)
    results: list[EntityMetrics] = []
    units: list[Unit] = []

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
            units.append(_unit(entity, measured, target, profile))
        results.append(measured)

    _mark_shredding(results, units, profile)
    _mark_class_aggregates(results, entities)
    return results


def _mark_class_aggregates(results: list[EntityMetrics], entities: list[Entity]) -> None:
    """Record NOM and WMC on every class, the way `_mark_shredding` records its total.

    Both are **file-local and exact**, which is what lets them gate at all: a class's method
    list is a containment fact this tree settles, so ADR-0002's rule that only an exact
    measurement may block is satisfied without any resolution.

    LCOM4 is deliberately absent. It needs the field-access model, which needs the scope
    tree, which `measure_file` is not given; it stays in `oxn classes`, reported and ungated.
    """
    totals = _class_totals(results, entities)
    for measured in results:
        found = totals.get(measured.entity_id)
        if found is not None:
            measured.add(MetricValue("nom", found[0]))
            measured.add(MetricValue("wmc", found[1]))


def _class_totals(
    results: list[EntityMetrics], entities: list[Entity]
) -> dict[str, tuple[int, float]]:
    """Class entity id -> (method count, summed cyclomatic complexity).

    WMC is weighted by *cyclomatic* complexity, per `docs/metrics.md` section 3.6 and for the
    reason `tests/test_class_scope_evasion.py` measures: cognitive weights fall under
    extraction, so a cognitive WMC reports an improvement exactly when work is being spread.

    Every class gets an entry, including one with no methods. A metric that is absent for the
    easy cases and present for the hard ones is a metric whose absence means two things.
    """
    parents = {entity.id: entity.parent_id for entity in entities}
    totals = {entity.id: [0, 0.0] for entity in entities if entity.kind in CLASS_KINDS}
    for measured in results:
        owner = parents.get(measured.entity_id)
        if measured.kind is not EntityKind.METHOD or owner is None:
            continue
        carried = totals.get(owner)
        if carried is not None:
            carried[0] += 1
            carried[1] += measured.get("cyclomatic_complexity") or 0.0
    return {owner: (int(count), weight) for owner, (count, weight) in totals.items()}


def _unit(entity: Entity, measured: EntityMetrics, node: Node, profile: LanguageProfile) -> Unit:
    """Reduce a measured callable to what the shredding question needs."""
    return Unit(
        entity_id=entity.id,
        name=entity.name or "",
        score=measured.get("cognitive_complexity") or 0.0,
        calls=tuple(callee_names(node, profile)),
    )


def _mark_shredding(
    results: list[EntityMetrics], units: list[Unit], profile: LanguageProfile
) -> None:
    """Record each cluster root's total, so the ceiling check can see it like any metric.

    Exact, not a bound: it states what this tree holds, rather than estimating the score an
    inlined version would have. See `oxn.metrics.shredding` for why that distinction matters.
    """
    clusters = cluster_totals(units, profile)
    if not clusters:
        return
    for measured in results:
        cluster = clusters.get(measured.entity_id)
        if cluster is not None:
            measured.add(MetricValue(METRIC, cluster.total, explanation=_shred_note(cluster)))


def _shred_note(cluster: Cluster) -> tuple[str, ...]:
    """Name the helpers, so the diagnostic says what to put back rather than only a number."""
    return (
        f"{len(cluster.helpers)} dedicated helpers -- private, trivial, each called once "
        f"here -- carry this work: {', '.join(cluster.helpers)}",
        "Splitting work across helpers that exist only for one caller does not raise the "
        "budget. Simplify the logic, or give the helpers a reason to exist independently.",
    )


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
    measured.add(MetricValue("parameter_count", _parameter_count(entity, profile)))

    volume = measured.get("halstead_volume") or 0.0
    sloc = int(measured.get("sloc") or 0)
    density = measured.get("comment_density") or 0.0
    index = maintainability_index(volume, cyclomatic, sloc, density)
    # Reported for compatibility, never gated on -- see docs/metrics.md section 3.7.
    measured.add(MetricValue("maintainability_index", round(index.visual_studio, 2)))


def _parameter_count(entity: Entity, profile: LanguageProfile) -> int:
    """Declared parameters, not counting a method's implicit receiver.

    `self` is not something a caller passes, so charging a method for it makes the
    "Long Parameter List" ceiling mean five for a function and four for a method -- the
    kind of quiet per-construct divergence OXN exists to avoid. Only a *method's* first
    parameter is eligible, so a plain function with an argument called `self` is unaffected,
    and so is a `@staticmethod`, whose first parameter is not conventionally named this way.
    """
    parameters = entity.attrs.get("parameters", [])
    if entity.kind is not EntityKind.METHOD or not parameters:
        return len(parameters)
    receiver = parameters[0] in profile.receiver_names
    return len(parameters) - 1 if receiver else len(parameters)


def callee_names(node: Node, profile: LanguageProfile) -> list[str]:
    """Bare names this callable calls, in its own body only.

    Nested definitions are not descended into: their calls are their own, and counting them
    twice would make a genuinely dedicated helper look like it had two callers.

    The name is the last segment of the callee expression, so ``self._helper`` and
    ``_helper`` are the same name. That is deliberately loose -- two same-named methods on
    different classes collide -- and the collision is handled by declining to fire, never by
    guessing which definition was meant.
    """
    spec = profile.metrics.cognitive
    if not spec.call_kinds:
        return []
    names: list[str] = []
    stack = list(node.named_children)
    while stack:
        current = stack.pop()
        if current.type in profile.function_like:
            continue
        if current.type in spec.call_kinds:
            name = _callee_name(current, spec.callee_field)
            if name:
                names.append(name)
        stack.extend(current.named_children)
    return names


def _callee_name(call: Node, callee_field: str) -> str:
    callee = call.child_by_field_name(callee_field)
    if callee is None or callee.text is None:
        return ""
    text = callee.text.decode("utf-8", "replace")
    for separator in (".", "::", "->"):
        text = text.rpartition(separator)[2]
    return text.strip()


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
