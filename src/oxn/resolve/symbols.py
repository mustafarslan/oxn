"""L1: a project symbol table stitched from L0's per-file scopes.

L0 answers "what does this name mean in this file". L1 answers "and which file declares it",
by following the import edges the dependency graph already resolved. That is enough for
in-repo class hierarchies and for calls whose name is unique across the project.

Where it stops is the point: a call through a variable whose type is inferred
(``repo.save()``) is invisible without type information, so L1 offers *candidates* with a
confidence, never a single confident answer. The L0/L1-versus-L2 table is what turns that
statement into a number.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from oxn.graph.model import Resolution

if TYPE_CHECKING:  # pragma: no cover
    from oxn.graph.depgraph import DependencyGraph
    from oxn.graph.model import Entity
    from oxn.resolve.scopes import ScopeTree


@dataclass(frozen=True, slots=True)
class ResolvedName:
    """What L1 believes a name refers to."""

    name: str
    entity_id: str
    qualified_name: str
    file_path: str
    resolution: Resolution
    #: ``1 / number of candidates``. A common method name resolves to many, and saying so is
    #: the difference between an approximation and a wrong answer.
    confidence: float = 1.0

    @property
    def is_certain(self) -> bool:
        return self.confidence >= 1.0


@dataclass
class ProjectSymbols:
    """Every declaration in the analysed tree, indexed for lookup."""

    #: file -> declared name -> entity
    by_file: dict[str, dict[str, Entity]] = field(default_factory=dict)
    #: bare name -> every entity declaring it, across the project
    by_name: dict[str, list[Entity]] = field(default_factory=lambda: defaultdict(list))
    #: file -> local alias -> import specifier
    aliases: dict[str, dict[str, str]] = field(default_factory=dict)
    #: file -> imported files
    imports: dict[str, set[str]] = field(default_factory=dict)

    def declarations_in(self, path: str) -> dict[str, Entity]:
        return self.by_file.get(path, {})

    def resolve_call(self, path: str, name: str) -> ResolvedName | None:
        """Best guess at what a call to ``name`` from ``path`` reaches.

        Preference order, each strictly better evidence than the next:

        1. a declaration in the calling file;
        2. a declaration in a file this one imports;
        3. a unique declaration anywhere in the project;
        4. one of several same-named declarations, reported with confidence < 1.
        """
        local = self.by_file.get(path, {}).get(name)
        if local is not None:
            return ResolvedName(name, local.id, local.qualified_name, path, Resolution.L0)

        for imported in sorted(self.imports.get(path, ())):
            found = self.by_file.get(imported, {}).get(name)
            if found is not None:
                return ResolvedName(name, found.id, found.qualified_name, imported, Resolution.L1)

        candidates = self.by_name.get(name, [])
        if len(candidates) == 1:
            only = candidates[0]
            return ResolvedName(name, only.id, only.qualified_name, only.file_path, Resolution.L1)
        if candidates:
            first = candidates[0]
            return ResolvedName(
                name,
                first.id,
                first.qualified_name,
                first.file_path,
                Resolution.L1,
                confidence=1 / len(candidates),
            )
        return None


def build_project_symbols(
    entities_by_file: dict[str, list[Entity]],
    scopes_by_file: dict[str, ScopeTree],
    graph: DependencyGraph | None = None,
) -> ProjectSymbols:
    """Index every declaration, plus the import edges that make cross-file lookup possible."""
    symbols = ProjectSymbols()

    for path, entities in entities_by_file.items():
        declared: dict[str, Entity] = {}
        for entity in entities:
            if entity.name is None or entity.kind.value == "module":
                continue
            # A method belongs to its class, not to the file's top level, so only
            # file-level declarations are reachable by a bare name.
            if entity.parent_id is None or _is_top_level(entity, entities):
                declared.setdefault(entity.name, entity)
            symbols.by_name[entity.name].append(entity)
        symbols.by_file[path] = declared

    for path, tree in scopes_by_file.items():
        symbols.aliases[path] = dict(tree.import_aliases)

    if graph is not None:
        symbols.imports = {path: set(targets) for path, targets in graph.files.items()}

    return symbols


def _is_top_level(entity: Entity, entities: list[Entity]) -> bool:
    """True when an entity's parent is the file's module entity."""
    parents = {other.id: other for other in entities}
    parent = parents.get(entity.parent_id or "")
    return parent is not None and parent.kind.value == "module"
