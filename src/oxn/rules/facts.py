"""Project the code graph, and the config, into the relations rules join against.

ADR-0005's rule: **nothing here parses, walks a CST or computes a metric.** Every tuple is
a projection of something the store or `oxn.yaml` already holds. A rule needing a fact that
does not exist is a finding to record, not a tree walk to add -- that is what keeps the
hook path affordable and the relation list small enough for a Datalog engine to load.

Two scopes, because two of the relations are expensive. `entity`, `metric`, `layer` and the
constant tables answer for a single file straight out of SQLite. `imports` and `cycle` need
the whole tree parsed, which is why contracts are `--deep`-only, and why a rule's scope is
derived from the relations its body touches rather than declared beside it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from oxn.config import CALLABLE_KINDS, CLASS_KINDS, FILE_KINDS, GATED_METRICS
from oxn.rules.model import Facts

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping

    from oxn.config import Config
    from oxn.graph.depgraph import DependencyGraph
    from oxn.graph.model import Entity, MetricValue
    from oxn.graph.store import GraphStore


def file_facts(
    store: GraphStore, paths: list[str], settings: Config, layer_of: dict[str, str | None]
) -> Facts:
    """Everything a file-scoped rule can join: entities, their measurements, ceilings."""
    facts = Facts()
    _constants(facts)
    for path in paths:
        if settings.is_advisory(path):
            continue
        _one_file(facts, store, path, layer_of.get(path))
        _ceilings(facts, settings, path, layer_of.get(path))
        _fed_classes(facts, store, settings, path, layer_of)
    return facts


def _one_file(facts: Facts, store: GraphStore, path: str, layer: str | None) -> None:
    entities = {entity.id: entity for entity in store.entities_for(path)}
    if layer is not None:
        facts.add("layer", (path, layer))
    for entity_id, values in store.measurements_for(path).items():
        entity = entities.get(entity_id)
        if entity is not None:
            _emit(facts, entity, path, values)


def _emit(
    facts: Facts, entity: Entity, path: str, values: Mapping[str, MetricValue]
) -> None:
    """One entity and every number measured on it, as rows."""
    facts.add(
        "entity",
        (entity.id, path, entity.kind.value, entity.qualified_name, entity.start_line),
    )
    for key, measured in values.items():
        facts.add("metric", (entity.id, key, measured.value, measured.exactness, measured.bound))
        # ADR-0002's gate policy as data, and derived from `MetricValue` itself so the
        # rules and the property cannot drift apart.
        facts.add("blocking", (measured.exactness, measured.bound, measured.can_block_ceiling))
        # Always a row, empty tuple included: a positive join must not silently drop
        # findings for metrics that happen to carry no trail.
        facts.add("explanation", (entity.id, key, tuple(measured.explanation)))


def _fed_classes(
    facts: Facts,
    store: GraphStore,
    settings: Config,
    path: str,
    layer_of: Mapping[str, str | None],
) -> None:
    """Classes in sibling files that *this* file declares methods for.

    A finding is reported for the entities in the files the run was given, and a class entity
    lives where its type is declared. Go declares a method at file scope with a receiver, so
    a thirteenth method added in a sibling file breached `methods_per_class` on an edit the
    hook never saw: `--deep` failed and `oxn check more.go` passed. The type's own file was
    already correct -- `aggregate_classes` completes the package -- so what was missing was
    not the number but a reason to look at it.

    Only the owning class is added, never the sibling's other entities: an unrelated long
    function in that file is not something this edit should be asked to fix.
    """
    from oxn.metrics.coupling import classes_fed_by

    entities = _package_entities(store, settings, path)
    for owner in classes_fed_by(list(entities.values()), path):
        entity = entities[owner]
        values = store.measurements_for(entity.file_path).get(owner, {})
        _emit(facts, entity, entity.file_path, values)
        _ceilings(facts, settings, entity.file_path, layer_of.get(entity.file_path))


def _package_entities(store: GraphStore, settings: Config, path: str) -> dict[str, Entity]:
    """Everything the edited file's directory declares, or nothing when it cannot matter.

    Two cheap refusals before the expensive read. A file that declares no method is not
    feeding a class anywhere, and a directory holding one file has no sibling to feed. What
    is left costs one `entities_for` per sibling: measured on the largest package in the
    corpora -- ESLint's `lib/rules`, 293 files -- **14 ms** against a 200 ms hook budget.
    The method guard does not save that one, because a rule module does declare methods; it
    saves the files that have none, which is most of them.
    """
    from oxn.graph.model import EntityKind

    if not any(entity.kind is EntityKind.METHOD for entity in store.entities_for(path)):
        return {}
    package = path.rpartition("/")[0]
    members = [
        known
        for known in store.known_paths()
        if known.rpartition("/")[0] == package and not settings.is_advisory(known)
    ]
    if len(members) < 2:
        return {}
    return {entity.id: entity for member in members for entity in store.entities_for(member)}


def _ceilings(facts: Facts, settings: Config, path: str, layer: str | None) -> None:
    """One row per (rule, path): the limit that rule applies to that file.

    Keyed by *rule* rather than metric key, because `function_sloc` and `file_sloc` share
    the metric `sloc` and differ only in their limit and the kinds they apply to.
    """
    for rule in settings.ceilings:
        gate = GATED_METRICS[rule]
        facts.add("ceiling", (rule, path, float(settings.ceiling_for(gate.follows or rule, layer))))


def _constants(facts: Facts) -> None:
    """The closed tables: which kinds are callables, which are files, which own methods."""
    facts.add("callable", *[(kind,) for kind in sorted(CALLABLE_KINDS)])
    facts.add("file_kind", *[(kind,) for kind in sorted(FILE_KINDS)])
    facts.add("class_kind", *[(kind,) for kind in sorted(CLASS_KINDS)])


def graph_facts(facts: Facts, graph: DependencyGraph, settings: Config) -> None:
    """Add the repository-scoped relations: imports, layers, and the contract tables.

    Separate from `file_facts` because building `graph` parses the whole tree. Calling this
    is the decision to pay for `--deep`.
    """
    from oxn.graph.contracts import assign_layers

    # Both ends of every edge, not just the files that were parsed. Every contract rule
    # joins `layer(Src, _)` *and* `layer(Dst, _)`, so a target with no layer fact makes the
    # rule silently unable to fire. Whole-tree runs never notice -- everything is parsed, so
    # every target is already a key -- but the hook parses one file and would otherwise
    # check its imports against a table that knows only itself.
    placed = set(graph.files) | {target for targets in graph.files.values() for target in targets}
    assignment = assign_layers(sorted(placed), settings.layers)
    for path, layer in assignment.items():
        if layer is not None:
            facts.add("layer", (path, layer))
    for source, targets in graph.files.items():
        for target in targets:
            facts.add("imports", (source, target))
            # The label a finding is keyed by, built here rather than in a head: Datalog has
            # no string functions, so anything derived from values is projected at fact time.
            facts.add("edge_label", (source, target, f"{source} -> {target}"))
    # `placed` again, and for a sharper reason: `_entrypoint_facts` glob-matches a
    # `deep_import` contract's declared entry points against these paths. Pass only the
    # parsed files and the hook — which parses one — finds no entry point, so every import
    # into the package looks like it bypassed one. An empty allow-list is not a strict
    # check, it is a false positive.
    _contract_facts(facts, settings, placed)
    _cycle_facts(facts, graph, settings)


def _cycle_facts(facts: Facts, graph: DependencyGraph, settings: Config) -> None:
    """Which import edges close a ring, for each `acyclic` contract that asked.

    Computed here rather than in a rule because Datalog has no fixpoint over *components*:
    the SCCs are found once with Tarjan and projected as ordinary rows, the same treatment
    `entrypoint_match` gives a glob.
    """
    for contract in settings.contracts:
        if contract.kind == "acyclic":
            rows = _ring_edges(graph, deferred=contract.deferred)
            facts.add("cycle", *[(contract.name, src, dst) for src, dst in rows])


def _ring_edges(graph: DependencyGraph, *, deferred: bool) -> list[tuple[str, str]]:
    """Every file edge whose two ends sit in one strongly connected component.

    **Two graphs, and the contract picks.** By default the ring is looked for among the
    imports that run when a module loads. A Python or CommonJS import inside a function body
    is the standard *fix* for an import cycle, so counting it finds a ring exactly where one
    was already broken -- OXN's own tree is 9 components on all imports and 2 on these.
    `deferred: true` asks the stricter "may not reach itself by any path" on purpose.
    """
    from oxn.graph.algos import cycles

    components = graph.components if deferred else graph.hard_components
    ringed = {member for cycle in cycles(components) for member in cycle}
    if not ringed:
        return []
    edges = graph.files if deferred else graph.hard_files
    return [
        (source, target)
        for source, targets in edges.items()
        for target in targets
        if _crosses_the_ring(graph, ringed, source, target)
    ]


def _crosses_the_ring(
    graph: DependencyGraph, ringed: set[str], source: str, target: str
) -> bool:
    """An edge between two *different* components that are both in one ring."""
    src_component = graph.component_of(source)
    dst_component = graph.component_of(target)
    return src_component != dst_component and {src_component, dst_component} <= ringed


def _contract_facts(facts: Facts, settings: Config, targets: set[str]) -> None:
    """`oxn.yaml`'s contracts, compiled to relations rather than evaluator special cases."""
    for contract in settings.contracts:
        if contract.kind == "layered":
            _layered_facts(facts, contract)
        elif contract.kind == "forbidden":
            for target in contract.forbidden:
                facts.add("forbidden_edge", (contract.name, contract.source, target))
        elif contract.kind == "independence":
            facts.add("independent", *[(contract.name, m) for m in contract.modules])
        elif contract.kind == "deep_import":
            facts.add("package", (contract.name, contract.package))
            _entrypoint_facts(facts, contract, targets)


def _entrypoint_facts(facts: Facts, contract: object, targets: set[str]) -> None:
    """Which files *are* an entry point, matched here rather than in a rule.

    A glob is a function, and Datalog has none. Resolving the pattern against the known
    paths at projection time keeps the rule a pure join and keeps the built-in surface at
    relations. It also means a pattern matching nothing is visible as an empty relation
    rather than as a rule that quietly never fires.
    """
    from fnmatch import fnmatch

    name = contract.name  # type: ignore[attr-defined]
    for pattern in contract.allowed_entrypoints:  # type: ignore[attr-defined]
        facts.add("entrypoint_match", *[(name, path) for path in targets if fnmatch(path, pattern)])


def _layered_facts(facts: Facts, contract: object) -> None:
    """Each layer may depend on every layer below it; those pairs are the allowed edges."""
    order = contract.order  # type: ignore[attr-defined]
    name = contract.name  # type: ignore[attr-defined]
    facts.add("in_contract", *[(name, layer) for layer in order])
    facts.add(
        "allowed_edge",
        *[
            (name, order[higher], order[lower])
            for higher in range(len(order))
            for lower in range(higher + 1, len(order))
        ],
    )
