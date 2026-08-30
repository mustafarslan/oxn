"""Build the dependency graph an analysed tree actually has.

Three stages, each testable alone: extract imports per file, resolve their specifiers to
files, then aggregate file-level edges to whatever component granularity the caller asks
for. The default granularity is the directory, because that is the modularisation almost
every project already declares; explicit layer globs arrive with ``oxn.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from oxn.graph.imports import extract_imports
from oxn.graph.resolve import ResolutionContext, resolve_import

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable, Iterable, Sequence

    from oxn.graph.imports import RawImport

#: Import kinds that create *runtime* coupling. A `type_only` import is erased at compile
#: time, so counting it would misstate exactly what Martin's metrics are about.
RUNTIME_KINDS = frozenset({"value", "wildcard", "side_effect", "dynamic"})


@dataclass(frozen=True, slots=True)
class UnresolvedImport:
    """An import OXN could not place inside the tree.

    Never discarded: dropping these silently is what turns a layer gate into a
    false-negative machine (``docs/metrics.md`` section 4.1).
    """

    source: str
    specifier: str
    kind: str
    line: int


@dataclass
class DependencyGraph:
    """File- and component-level dependencies for an analysed tree."""

    #: file -> imported files, inside the tree only.
    files: dict[str, set[str]] = field(default_factory=dict)
    #: component -> depended-on components, with edge multiplicity.
    components: dict[str, set[str]] = field(default_factory=dict)
    weights: dict[tuple[str, str], int] = field(default_factory=dict)
    unresolved: list[UnresolvedImport] = field(default_factory=list)
    #: file -> component, so a violation can name the file that caused it.
    membership: dict[str, str] = field(default_factory=dict)
    #: Every import seen, including external ones, for reporting.
    import_count: int = 0
    external_count: int = 0

    @property
    def edge_count(self) -> int:
        return sum(len(targets) for targets in self.components.values())

    def component_of(self, path: str) -> str:
        return self.membership.get(path, path)


def directory_component(path: str) -> str:
    """Default granularity: the containing directory, or ``<root>`` at the top level."""
    parent = str(PurePosixPath(path).parent)
    return "<root>" if parent in {"", "."} else parent


def build_dependency_graph(
    root: Path,
    files: Sequence[Path],
    *,
    component_of: Callable[[str], str] = directory_component,
    include_type_only: bool = False,
) -> DependencyGraph:
    """Parse, resolve and aggregate. ``root`` anchors every path in the result."""
    from oxn.languages import get_parser
    from oxn.profiles import profile_for_path

    relative_paths = [_relative(path, root) for path in files]
    context = ResolutionContext.build(root, set(relative_paths))

    graph = DependencyGraph()
    kinds = RUNTIME_KINDS | ({"type_only"} if include_type_only else set())

    for path, relative in zip(files, relative_paths, strict=True):
        profile = profile_for_path(str(path))
        if profile is None:
            continue
        tree = get_parser(profile.name).parse(path.read_bytes())
        if tree.root_node.has_error:
            continue

        graph.files.setdefault(relative, set())
        graph.membership[relative] = component_of(relative)

        for raw in extract_imports(tree.root_node, profile):
            graph.import_count += 1
            resolved = resolve_import(relative, raw, context, profile.name)
            if not resolved.resolved:
                graph.external_count += 1
                if _is_internal_looking(raw):
                    graph.unresolved.append(
                        UnresolvedImport(relative, raw.specifier, raw.kind, raw.line)
                    )
                continue
            if raw.kind not in kinds:
                continue
            for target in resolved.targets:
                if target != relative:
                    graph.files[relative].add(target)

    _aggregate(graph, component_of)
    return graph


def _is_internal_looking(raw: RawImport) -> bool:
    """Only relative imports are *expected* to resolve.

    A bare specifier is usually a third-party package, and reporting every one of those as
    unresolved would bury the handful that matter.
    """
    return raw.is_relative


def _aggregate(graph: DependencyGraph, component_of: Callable[[str], str]) -> None:
    for source, targets in graph.files.items():
        source_component = component_of(source)
        graph.components.setdefault(source_component, set())
        for target in targets:
            target_component = graph.membership.get(target) or component_of(target)
            graph.components.setdefault(target_component, set())
            if target_component == source_component:
                continue
            graph.components[source_component].add(target_component)
            key = (source_component, target_component)
            graph.weights[key] = graph.weights.get(key, 0) + 1


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - a path outside the tree
        return path.resolve().as_posix()


def component_type_counts(
    store_entities: Iterable[tuple[str, str, bool]], component_of: Callable[[str], str]
) -> dict[str, tuple[int, int]]:
    """``(abstract, total)`` type counts per component, for Martin's abstractness.

    Takes ``(file path, entity kind, is_abstract)`` triples so the caller controls where
    they come from -- the cache in normal use, a fixture in tests.
    """
    counts: dict[str, list[int]] = {}
    for path, kind, is_abstract in store_entities:
        if kind not in {"class", "interface"}:
            continue
        bucket = counts.setdefault(component_of(path), [0, 0])
        bucket[1] += 1
        if is_abstract or kind == "interface":
            bucket[0] += 1
    return {name: (abstract, total) for name, (abstract, total) in counts.items()}
