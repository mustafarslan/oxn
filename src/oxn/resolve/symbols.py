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

    #: file -> the imported local names that provably name nothing in this tree.
    external_aliases: dict[str, frozenset[str]] = field(default_factory=dict)
    #: file -> declared name -> **every** entity in that file declaring it.
    #:
    #: A list, not one entity, because "one declaration per name per file" is a Python and
    #: TypeScript assumption that Go breaks flatly: a method is a *top-level* declaration
    #: carrying its receiver, so `Counter.With`, `Gauge.With`, `Timing.With` and
    #: `Histogram.With` are four file-level `With`s in one file. Collapsing them with
    #: `setdefault` kept the first and answered every call with it at **confidence 1.0** --
    #: correct one time in four, and confident every time.
    by_file: dict[str, dict[str, list[Entity]]] = field(default_factory=dict)
    #: bare name -> every entity declaring it, across the project
    by_name: dict[str, list[Entity]] = field(default_factory=lambda: defaultdict(list))
    #: file -> local alias -> import specifier
    aliases: dict[str, dict[str, str]] = field(default_factory=dict)
    #: file -> imported files
    imports: dict[str, set[str]] = field(default_factory=dict)

    def declarations_in(self, path: str) -> dict[str, Entity]:
        """The unambiguous file-level declarations, one per name.

        A name declared more than once in the file has no single answer and is omitted
        rather than resolved to whichever came first.
        """
        return {
            name: found[0] for name, found in self.by_file.get(path, {}).items() if len(found) == 1
        }

    def resolve_call(
        self, path: str, name: str, qualifier: str | None = None
    ) -> ResolvedName | None:
        """Best guess at what a call to ``name`` from ``path`` reaches.

        Preference order, each strictly better evidence than the next:

        1. a declaration in the calling file;
        2. a declaration in a file this one imports;
        3. a unique declaration anywhere in the project;
        4. one of several same-named declarations, reported with confidence < 1.

        ``qualifier`` is the name a call was written *through* -- the `c` of `c.With()`, the
        `metrics` of `metrics.NewCounter()`, the `self` of `self.helper()` -- and ``None``
        for a bare `helper()`. Two rules follow from it, both measured rather than assumed:

        **A qualified call never takes step 1.** The calling file's own *top-level*
        declarations say nothing about what a receiver's type is or what another package
        contains, and answering from them was wrong essentially every time: on the pinned
        corpora, step 1 answering a qualified call scored 0/265 in Rust, 0/51 for Go's
        package-qualified calls, 0/2 in Python, and 91/142 for Go receivers -- against
        99.3%-100% for every later step. It is the single mechanism behind all 318
        confidently-wrong qualified answers.

        **A call through an import that reached no file in this tree resolves to nothing.**
        If `np` names `numpy` and `numpy` is not in the tree, `np.array()` is not an entity
        here, and any in-tree `array` is a coincidence. The grader cannot score these at all
        -- the oracle places them out of tree, so they are never graded -- but they reach
        `metrics.callgraph`, CBO and RFC. ADR-0002's ninth amendment has the numbers.
        """
        if qualifier is not None and qualifier in self.external_aliases.get(path, ()):
            return None
        if qualifier is None:
            # Ambiguity inside one file is still ambiguity, hence `1/n` rather than a bare
            # first answer: `metrics.callgraph` admits only confidence-1.0 edges below L2,
            # so anything less honest enters the call graph as ground truth.
            local = _answer(name, self.by_file.get(path, {}).get(name) or [], path, Resolution.L0)
            if local is not None:
                return local

        for imported in sorted(self.imports.get(path, ())):
            candidates = self.by_file.get(imported, {}).get(name) or []
            found = _answer(name, candidates, imported, Resolution.L1)
            if found is not None:
                return found

        anywhere = self.by_name.get(name, [])
        if not anywhere:
            return None
        return _answer(name, anywhere, anywhere[0].file_path, Resolution.L1)


def build_project_symbols(
    entities_by_file: dict[str, list[Entity]],
    scopes_by_file: dict[str, ScopeTree],
    graph: DependencyGraph | None = None,
) -> ProjectSymbols:
    """Index every declaration, plus the import edges that make cross-file lookup possible."""
    symbols = ProjectSymbols()
    for path, entities in entities_by_file.items():
        symbols.by_file[path] = _declared_in(entities, symbols)
    for path, tree in scopes_by_file.items():
        symbols.aliases[path] = dict(tree.import_aliases)
        symbols.external_aliases[path] = _external_aliases(tree, graph, path)
    if graph is not None:
        symbols.imports = {path: set(targets) for path, targets in graph.files.items()}
    return symbols


def _answer(
    name: str, candidates: list[Entity], file_path: str, resolution: Resolution
) -> ResolvedName | None:
    """One candidate list, reported with the confidence it has earned: ``1 / n``.

    Written once because it was written four times, and the fourth had drifted -- the
    project-wide unique case built a `ResolvedName` without the `confidence` keyword and got
    the default. Same value, but the rule was no longer in one place to change.
    """
    if not candidates:
        return None
    first = candidates[0]
    return ResolvedName(
        name,
        first.id,
        first.qualified_name,
        file_path,
        resolution,
        confidence=1 / len(candidates),
    )


def _external_aliases(tree: ScopeTree, graph: DependencyGraph | None, path: str) -> frozenset[str]:
    """Local names whose import was placed and reached nothing inside this tree.

    Only a *placed* specifier counts. An absent entry means resolution never ran on that
    specifier, which is not the same claim -- and it is the common case in Rust and Java,
    where `use std::fmt` records the alias `fmt -> std` while the import specifier is
    `std.fmt`. The two do not join, no entry is found, and the rule correctly declines to
    fire rather than declaring half a language external.
    """
    if graph is None:
        return frozenset()
    placed = graph.specifier_targets.get(path, {})
    return frozenset(
        name
        for name, source in tree.import_aliases.items()
        if source in placed and not placed[source]
    )


def _declared_in(entities: list[Entity], symbols: ProjectSymbols) -> dict[str, list[Entity]]:
    """One file's bare-name declarations, indexing every entity by name as it goes.

    Only *file-level* declarations answer to a bare name: a method belongs to its class, so
    `handle` in one class must not resolve a call to `handle` written in another. Every
    entity still enters `by_name`, which is what makes the project-wide fallback possible
    when a file-local lookup finds nothing.

    **Every** declaration of a name is kept, not the first. In Go a method is file-level and
    carries its receiver in the signature rather than in a parent scope, so one file
    routinely declares the same bare name several times -- and `setdefault` turned that into
    a single confident answer that was wrong as often as the receiver count.
    """
    declared: dict[str, list[Entity]] = {}
    for entity in entities:
        if entity.name is None or entity.kind.value == "module":
            continue
        if entity.parent_id is None or _is_top_level(entity, entities):
            declared.setdefault(entity.name, []).append(entity)
        symbols.by_name[entity.name].append(entity)
    return declared


def _is_top_level(entity: Entity, entities: list[Entity]) -> bool:
    """True when an entity's parent is the file's module entity."""
    parents = {other.id: other for other in entities}
    parent = parents.get(entity.parent_id or "")
    return parent is not None and parent.kind.value == "module"
