"""Join SCIP occurrences onto the code graph.

SCIP records *where every symbol appears*, but it does not say which of those appearances
are calls -- that is syntax, and syntax is what OXN already has. The call graph comes from a
**range join**: walk the CST for call nodes, take the callee's position, and look up which
symbol occupies it.

Positions on both sides are 0-based ``(line, column)``. The join deliberately does *not* go
through the stored entity rows: those span their wrapper (a decorated function starts at its
decorator), so their byte ranges do not line up with an occurrence on the name.

**What stays approximate even here.** SCIP resolves a call to the *declared* target. Virtual
dispatch, higher-order functions, reflection and DI containers all mean the symbol reached at
runtime may differ, so call-derived metrics are exact "over the resolved symbol set" and no
more (docs/metrics.md section 2.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from oxn.graph.model import Edge, EdgeKind, Provenance, Resolution

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator

    from tree_sitter import Node

    from oxn.graph.model import Entity
    from oxn.profiles.base import LanguageProfile
    from oxn.scip.index import ScipDocument, ScipOccurrence


def scoped(symbol: str, path: str) -> str:
    """Qualify a SCIP ``local`` symbol with the document that owns it.

    **A `local N` symbol is document-scoped by the SCIP specification**, and the numbering
    restarts in every file: nest's index has 628 documents that each define `local 0`. OXN
    keyed both its symbol table and its edge references on the bare symbol, so `local 0`
    resolved to whichever file was ingested last -- `symbols.symbol` is a PRIMARY KEY, and
    `resolve_edge_targets` joins on it with no file condition.

    That is not a near miss. On `typescript-nest` it manufactured **529 `calls` edges that
    point into a file the caller never mentions** -- 17% of every L2 edge resolved on that
    repository -- with `packages/common` calling `packages/platform-express`. L2 is the rung
    the other two are graded against, so a fabricated edge here is a fabricated fact
    everywhere downstream: `metrics.callgraph`, CBO, RFC, dead code.

    Qualifying makes the key say what the spec says. A same-file reference still resolves; a
    cross-file one cannot, which is correct, because a local has no cross-file meaning.
    """
    return f"{symbol} {path}" if symbol.startswith("local ") else symbol


@dataclass
class JoinResult:
    """Edges recovered for one file, plus the coverage that produced them."""

    edges: list[Edge]
    #: symbol -> entity id, for the definitions that matched an entity. A `local` key is
    #: qualified with `path`; see `scoped`.
    definitions: dict[str, str]
    #: The document these came from, needed to scope its local symbols.
    path: str = ""
    call_sites: int = 0
    joined_call_sites: int = 0
    definition_sites: int = 0
    joined_definition_sites: int = 0

    @property
    def call_coverage(self) -> float:
        return self.joined_call_sites / self.call_sites if self.call_sites else 0.0

    @property
    def definition_coverage(self) -> float:
        return (
            self.joined_definition_sites / self.definition_sites if self.definition_sites else 0.0
        )


def join_document(
    entities: list[Entity],
    profile: LanguageProfile,
    tree_root: Node,
    document: ScipDocument,
) -> JoinResult:
    """Recover ``CALLS``, ``EXTENDS`` and ``OVERRIDES`` edges for one file."""
    by_position = _index_occurrences(document)
    by_def_range = {
        tuple(entity.attrs["def_range"]): entity
        for entity in entities
        if "def_range" in entity.attrs
    }

    result = JoinResult(edges=[], definitions={}, path=document.relative_path)
    _map_definitions(by_position, by_def_range, profile, tree_root, result)
    _map_calls(by_position, by_def_range, profile, tree_root, result)
    _map_relationships(document, result)
    return result


def _index_occurrences(document: ScipDocument) -> dict[tuple[int, int], ScipOccurrence]:
    """``(line, column)`` -> occurrence, keyed on the occurrence's start."""
    return {
        (occurrence.start_line, occurrence.start_char): occurrence
        for occurrence in document.occurrences
    }


def _map_definitions(
    by_position: dict[tuple[int, int], ScipOccurrence],
    by_def_range: dict[tuple[int, int], Entity],
    profile: LanguageProfile,
    tree_root: Node,
    result: JoinResult,
) -> None:
    """Match each entity to the SCIP symbol declared at its name."""
    for definition, entity in _iter_definitions(profile, tree_root, by_def_range):
        # `profile.name_node`, not the name field: a callable bound to a name -- `const
        # handler = () => {}` -- is named by the identifier *outside* it, and SCIP puts its
        # definition occurrence there too. Reading the field alone left every one of them
        # unjoined, so they could be resolved by name and never graded.
        name_node = profile.name_node(definition)
        if name_node is None:
            continue
        result.definition_sites += 1
        occurrence = by_position.get(name_node.start_point)
        if occurrence is not None and occurrence.is_definition:
            result.definitions[scoped(occurrence.symbol, result.path)] = entity.id
            result.joined_definition_sites += 1


def _map_calls(
    by_position: dict[tuple[int, int], ScipOccurrence],
    by_def_range: dict[tuple[int, int], Entity],
    profile: LanguageProfile,
    tree_root: Node,
    result: JoinResult,
) -> None:
    call_kinds = profile.metrics.cognitive.call_kinds
    callee_field = profile.metrics.cognitive.callee_field
    if not call_kinds:
        return

    stack = [tree_root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type not in call_kinds:
            continue

        callee = node.child_by_field_name(callee_field)
        if callee is None:
            continue
        result.call_sites += 1

        # A qualified callee (`repo.save`) must resolve on its *last* name component. Its
        # start position belongs to the receiver, and `repo` is usually a local variable --
        # matching there produces an edge to the local instead of to the method.
        occurrence = by_position.get(_last_name_position(callee)) or by_position.get(
            callee.start_point
        )
        if occurrence is None:
            continue

        caller = _enclosing_entity(node, profile, by_def_range)
        if caller is None:
            continue

        result.joined_call_sites += 1
        result.edges.append(
            Edge(
                src_id=caller.id,
                kind=EdgeKind.CALLS,
                dst_ref=scoped(occurrence.symbol, result.path),
                provenance=Provenance.SCIP,
                resolution=Resolution.L2,
                attrs={"line": node.start_point[0] + 1},
            )
        )


def _map_relationships(document: ScipDocument, result: JoinResult) -> None:
    """``is_implementation`` gives inheritance and overrides without any syntax work."""
    for symbol in document.symbols:
        source_id = result.definitions.get(scoped(symbol.symbol, result.path))
        if source_id is None:
            continue
        for relationship in symbol.relationships:
            if not relationship.is_implementation:
                continue
            kind = EdgeKind.OVERRIDES if "()." in symbol.symbol else EdgeKind.EXTENDS
            result.edges.append(
                Edge(
                    src_id=source_id,
                    kind=kind,
                    dst_ref=scoped(relationship.symbol, result.path),
                    provenance=Provenance.SCIP,
                    resolution=Resolution.L2,
                )
            )


def _iter_definitions(
    profile: LanguageProfile, tree_root: Node, by_def_range: dict[tuple[int, int], Entity]
) -> Iterator[tuple[Node, Entity]]:
    """Definition nodes that correspond to a stored entity."""
    stack = [tree_root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        entity = by_def_range.get((node.start_byte, node.end_byte))
        if entity is not None and profile.is_definition(node):
            yield node, entity


def _enclosing_entity(
    node: Node, profile: LanguageProfile, by_def_range: dict[tuple[int, int], Entity]
) -> Entity | None:
    """Nearest enclosing definition, by walking up the CST.

    Walking the tree is right and matching byte ranges against stored entities is not: an
    entity's recorded span includes its wrapper, so a decorated function's range starts at
    the decorator and would never match its own definition node.
    """
    current = node.parent
    while current is not None:
        if profile.is_definition(current):
            entity = by_def_range.get((current.start_byte, current.end_byte))
            if entity is not None:
                return entity
        current = current.parent
    return None


def _last_name_position(callee: Node) -> tuple[int, int]:
    """Position of the final name in a qualified callee, e.g. the ``save`` in ``repo.save``."""
    current = callee
    while current.named_child_count:
        current = current.named_children[-1]
    return current.start_point
