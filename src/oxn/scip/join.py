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

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from oxn.graph.model import Edge, EdgeKind, EntityKind, Provenance, Resolution

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator

    from tree_sitter import Node

    from oxn.graph.model import Entity
    from oxn.profiles.base import LanguageProfile
    from oxn.scip.index import ScipDocument, ScipOccurrence


def is_document_local(symbol: str) -> bool:
    """Is this a SCIP ``local N`` symbol -- document-scoped, and carrying no name?

    Every other SCIP symbol ends in a *descriptor*, which spells out what the thing is
    called: ``... `no-unused-vars`/create().``. A ``local 4`` has none. It is how an indexer
    spells a binding the module never exports -- in JavaScript, a plain ``function foo()``
    or a ``const foo = require(...)`` at file scope -- and the number is an index into the
    document, not a name.

    The grader used to require that the oracle's descriptor tail match the name written at
    the call site, so that a mis-parsed descriptor could not arbitrate anything. Against a
    nameless symbol that check is not *failed*, it is **inapplicable**: there is no tail, so
    it can never match, and every call through a module-local binding was thrown away. On
    ESLint that was **5,036 of 5,055 exclusions** -- 38.18% of the corpus's call sites, and
    JavaScript's commonest call shape.

    | | graded | excluded | confident precision |
    |---|---|---|---|
    | requiring a tail | 8,185 | 38.18% | 100.0% |
    | this rule | 13,221 | 0.14% | 99.8% |

    The check did a second job as well -- confirming the occurrence is *for* the callee --
    and skipping it drops that too, so it was measured separately: on all 5,036 newly graded
    sites the occurrence's range equals the callee's last-name node exactly. The position
    lookup in `_last_name_position` establishes that on its own; the tail was never what was
    holding it up. Confident precision across 5,000 more sites moving 100.0% -> 99.8% is the
    other half of that evidence.

    This is the same shape as the incident `symbol_tail` records, where a filter meant to
    protect a measurement had quietly selected the subset it was measured on.
    """
    return symbol.startswith("local ")


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
class _Join:
    """The state one document's join carries between its passes.

    Assembled once rather than threaded as arguments: `_map_calls` and `_map_references` had
    grown to six parameters each, and they are not six things -- they are one document being
    read four ways.
    """

    by_position: dict[tuple[int, int], ScipOccurrence]
    by_def_range: dict[tuple[int, int], Entity]
    profile: LanguageProfile
    tree_root: Node
    result: JoinResult
    #: Occurrence positions `_map_calls` matched, so `_map_references` can tell a call from
    #: a mention of the same name. SCIP marks definitions, imports, reads and writes -- it
    #: does not mark calls, so this is the only way to know.
    consumed: set[tuple[int, int]] = field(default_factory=set)


@dataclass
class JoinResult:
    """Edges recovered for one file, plus the coverage that produced them."""

    edges: list[Edge]
    #: symbol -> entity id, for the definitions that matched an entity. A `local` key is
    #: qualified with `path`; see `scoped`.
    definitions: dict[str, str]
    #: The document these came from, needed to scope its local symbols.
    path: str = ""
    #: The file's own entity, which owns any call written at module scope.
    module: Entity | None = None
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

    result = JoinResult(
        edges=[],
        definitions={},
        path=document.relative_path,
        module=next(
            (entity for entity in entities if entity.kind in {EntityKind.FILE, EntityKind.MODULE}),
            None,
        ),
    )
    join = _Join(by_position, by_def_range, profile, tree_root, result)
    _map_definitions(join)
    _map_calls(join)
    _map_references(join, document)
    _map_relationships(document, result)
    return result


def _index_occurrences(document: ScipDocument) -> dict[tuple[int, int], ScipOccurrence]:
    """``(line, column)`` -> occurrence, keyed on the occurrence's start."""
    return {
        (occurrence.start_line, occurrence.start_char): occurrence
        for occurrence in document.occurrences
    }


def _map_definitions(join: _Join) -> None:
    """Match each entity to the SCIP symbol declared at its name."""
    by_position, by_def_range = join.by_position, join.by_def_range
    profile, tree_root, result = join.profile, join.tree_root, join.result
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


def _map_calls(join: _Join) -> None:
    by_position, by_def_range = join.by_position, join.by_def_range
    profile, tree_root, result, consumed = join.profile, join.tree_root, join.result, join.consumed
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

        position, occurrence = _occurrence_at(callee, by_position)
        if occurrence is None:
            continue

        # A call written at module scope has no enclosing definition, and dropping it lost
        # a real edge: `if __name__ == "__main__": _main()` is how a script reaches its own
        # entry point, and every `_main` so called was reported as dead code. The file's own
        # entity is the caller, which is what executes it.
        caller = _enclosing_entity(node, profile, by_def_range) or result.module
        if caller is None:
            continue

        # Recorded so `_map_references` can tell a call from a mention of the same name.
        consumed.add(position)
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


def _map_references(join: _Join, document: ScipDocument) -> None:
    """``REFERENCES`` edges: a symbol *mentioned* where it is not called.

    **A callable used as a value has no call node, and reachability that only follows calls
    cannot see it.** `_HANDLERS = (_ignored, _else_if, ...)` is how this repository dispatches
    cognitive-complexity handlers, and `{"python": _python_statement, ...}` is how it
    dispatches import parsers -- neither writes a call, so nothing reached those functions and
    everything downstream of them was reported dead. Measured on OXN's own `src/` before this
    existed: 69 dead-code candidates, 38 of them roots of the dead forest, and **36 of those
    38 were a reference rather than a call**.

    Three exclusions, each of which would otherwise make the edge a lie:

    * **Definitions.** The name at its own `def` is not a use of it.
    * **Calls**, via ``consumed``. SCIP does not mark a call occurrence -- the role bits know
      about reads, writes, imports and definitions, and nothing else -- so the only way to
      know is that `_map_calls` already matched that position, which is why it records them.
    * **Imports**, via ``roles``. An import is a mention and not a use: `_python_module_ref`
      is imported by the module that dispatches to it, and counting that would let every
      importable name reach itself.

    Kept out of `CallGraph` entirely and used only for reachability. Fan-in, fan-out and the
    recursion increment are counts of *calls*, and a function named in a handler table has
    not been called once.
    """
    by_def_range, profile = join.by_def_range, join.profile
    tree_root, result, consumed = join.tree_root, join.result, join.consumed
    for occurrence in document.occurrences:
        position = (occurrence.start_line, occurrence.start_char)
        if occurrence.is_definition or occurrence.is_import or position in consumed:
            continue
        node = tree_root.descendant_for_point_range(position, position)
        source = (
            _enclosing_entity(node, profile, by_def_range) if node is not None else None
        ) or result.module
        if source is None:
            continue
        result.edges.append(
            Edge(
                src_id=source.id,
                kind=EdgeKind.REFERENCES,
                dst_ref=scoped(occurrence.symbol, result.path),
                provenance=Provenance.SCIP,
                resolution=Resolution.L2,
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


def _occurrence_at(
    callee: Node, by_position: dict[tuple[int, int], ScipOccurrence]
) -> tuple[tuple[int, int], ScipOccurrence | None]:
    """The occurrence a callee resolves to, and the position it was found at.

    **A qualified callee (`repo.save`) resolves on its *last* name component, and on nothing
    else.** Its start position belongs to the receiver, and `repo` is usually a local
    variable, so matching there produces an edge to the local instead of to the method.

    There used to be a fallback to `callee.start_point` "for a bare name", and it was dead in
    the only direction that mattered: for a bare callee the last name *is* the start, so the
    first lookup already answers. Measured over OXN's own tree, 8,754 call sites: the fallback
    fired **840 times, always on a qualified callee whose method SCIP could not place, and
    every one of those 840 resolved to the receiver** -- 587 to a local variable and 253 to a
    parameter symbol. It was reintroducing precisely the defect the paragraph above exists to
    prevent, and counting each one as a joined call site, so `call_coverage` was reporting
    them as successes. A method SCIP could not place is a call OXN cannot name, and declining
    to name it is the honest answer.

    The position is returned alongside because `_map_calls` records it: it is what lets
    `_map_references` tell a call from a mention.
    """
    position = _last_name_position(callee)
    return position, by_position.get(position)


def _last_name_position(callee: Node) -> tuple[int, int]:
    """Position of the final name in a qualified callee, e.g. the ``save`` in ``repo.save``."""
    current = callee
    while current.named_child_count:
        current = current.named_children[-1]
    return current.start_point
