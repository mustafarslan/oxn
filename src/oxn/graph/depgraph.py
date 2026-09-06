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

    from tree_sitter import Node

    from oxn.graph.imports import RawImport
    from oxn.profiles.base import LanguageProfile

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
    #: file -> specifier -> the in-tree files it reached. An **empty** tuple is a placed
    #: specifier that reached nothing -- a third-party package -- and an absent key is one
    #: never resolved at all. The difference is the whole value of this table: only the
    #: first proves a call through that name cannot land in this tree.
    specifier_targets: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)
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
    known: set[str] | None = None,
) -> DependencyGraph:
    """Parse, resolve and aggregate. ``root`` anchors every path in the result.

    ``known`` separates *what is resolved against* from *what is parsed*, which is what
    lets the hook check one file's imports. Resolution needs the whole tree's layout —
    ``import oxn.check`` is placeable only if something knows `src/oxn/check.py` exists —
    while parsing is what costs seconds. Passing the edited file in ``files`` and every
    repo-relative path in ``known`` buys the edited file's real edges for one parse plus a
    directory walk. Defaulting to the parsed set is the whole-tree call, unchanged.
    """
    relative_paths = [_relative(path, root) for path in files]
    context = ResolutionContext.build(root, known if known is not None else set(relative_paths))

    graph = DependencyGraph()
    kinds = RUNTIME_KINDS | ({"type_only"} if include_type_only else set())

    for path, relative in zip(files, relative_paths, strict=True):
        parsed = _parsed_tree(path)
        if parsed is None:
            continue
        profile, tree_root = parsed
        graph.files.setdefault(relative, set())
        graph.membership[relative] = component_of(relative)
        _add_imports(graph, _Scan(relative, tree_root, profile, context, kinds))

    _aggregate(graph, component_of)
    return graph


def _parsed_tree(path: Path) -> tuple[LanguageProfile, Node] | None:
    """The parse tree for one file, or None when it cannot contribute edges.

    A file with a syntax error is skipped rather than half-read: a partial tree yields a
    partial import list, and a *missing* edge silently weakens every architectural claim
    made from this graph.
    """
    from oxn.languages import get_parser
    from oxn.profiles import profile_for_path

    profile = profile_for_path(str(path))
    if profile is None:
        return None
    tree = get_parser(profile.name).parse(path.read_bytes())
    if tree.root_node.has_error:
        return None
    return profile, tree.root_node


@dataclass(frozen=True, slots=True)
class _Scan:
    """One file's imports, and everything needed to resolve them."""

    relative: str
    tree_root: Node
    profile: LanguageProfile
    context: ResolutionContext
    kinds: frozenset[str]


def _add_imports(graph: DependencyGraph, scan: _Scan) -> None:
    """Resolve every import in one file into edges, counting what does not resolve."""
    for raw in extract_imports(scan.tree_root, scan.profile):
        graph.import_count += 1
        resolved = resolve_import(scan.relative, raw, scan.context, scan.profile.name)
        graph.specifier_targets.setdefault(scan.relative, {})[raw.specifier] = resolved.targets
        if not resolved.resolved:
            _record_external(graph, scan.relative, raw, scan)
            continue
        if raw.kind not in scan.kinds:
            continue
        for target in resolved.targets:
            if target != scan.relative:
                graph.files[scan.relative].add(target)


def _record_external(graph: DependencyGraph, relative: str, raw: RawImport, scan: _Scan) -> None:
    """Unresolved is usually third-party; only the internal-looking ones are reported."""
    graph.external_count += 1
    if _is_internal_looking(raw, scan):
        graph.unresolved.append(UnresolvedImport(relative, raw.specifier, raw.kind, raw.line))


def _is_internal_looking(raw: RawImport, scan: _Scan) -> bool:
    """Is this an import that *should* have resolved inside the tree?

    A relative specifier always is. So is a bare specifier naming one of this repository's
    own workspace packages -- and that second case was silently absent: `@scope/pkg/moved`
    in a monorepo counted as a third-party dependency, indistinguishable from `express`.
    `docs/metrics.md` section 4.1 is explicit that a missing edge must be *visible*, and
    this was the one class of missing edge that looked exactly like correct behaviour.

    **So was every import in Go, Rust and Java**, for the same reason and on a far larger
    scale: a Go module path, a `crate::` path and a Java package all answered "not ours", so
    go-kit's 1,131 imports, ripgrep's 1,053 and petclinic's 471 were filed as third-party
    dependencies and produced **zero** in-tree edges with **zero** reported as missing. A
    layered contract over a Go repository passed unconditionally, because it had no edges to
    judge -- demonstrated in `tests/test_go_resolution.py`.

    Everything else is a package, and reporting every one of those would bury the handful
    that matter.
    """
    return (
        raw.is_relative
        or scan.context.is_workspace_specifier(raw.specifier)
        or scan.context.is_own_module(raw.specifier)
    )


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
