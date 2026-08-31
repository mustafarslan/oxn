"""The import graph against grimp."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.oracle


def _to_module(path: str) -> str:
    """Repo-relative path -> dotted module name, the way grimp names things."""
    trimmed = path[len("src/") :] if path.startswith("src/") else path
    trimmed = trimmed[:-3] if trimmed.endswith(".py") else trimmed
    if trimmed.endswith("/__init__"):
        trimmed = trimmed[: -len("/__init__")]
    return trimmed.replace("/", ".")


def _oxn_module_edges(
    root: Path, sources: Path, package: str, *, include_type_only: bool = False
) -> set[tuple[str, str]]:
    from oxn.graph.depgraph import build_dependency_graph
    from oxn.graph.sources import iter_source_files

    files = [
        path
        for path in iter_source_files([sources])
        if "/tests/" not in str(path) and path.name != "setup.py"
    ]
    graph = build_dependency_graph(root, files, include_type_only=include_type_only)
    edges = {
        (_to_module(source), _to_module(target))
        for source, targets in graph.files.items()
        for target in targets
    }
    return {
        (source, target)
        for source, target in edges
        if source.startswith(package) and target.startswith(package)
    }


@pytest.mark.oracle
def test_every_import_divergence_from_grimp_is_explained() -> None:
    """Two mechanisms, two definitions of "depends on", and both are defensible.

    Ours reads syntax; grimp executes imports. Agreement is strong evidence, and this
    comparison has already found one real modelling bug -- `from pkg import core` reaches the
    package *and* the submodule, and treating it as one target silently lost edges.

    Raw equality is the wrong assertion, for the same reason it was wrong for cyclomatic
    complexity: the two tools answer different questions in two enumerable places.

    * **grimp counts type-only imports; OXN does not.** An import inside `if TYPE_CHECKING:`
      is erased at runtime, so it creates no coupling of the kind Martin's metrics measure --
      and OXN already excluded TypeScript's `import type`. Counting Python's equivalent was
      an inconsistency, and while it lasted it manufactured two false layer-contract
      violations in this repository.
    * **OXN records the package as well as the submodule.** `from oxn import thresholds`
      executes `oxn/__init__.py`, so the dependency on the package is real; grimp reports
      only `oxn.thresholds`.

    Every divergence must reduce to one of those two rules. A third kind is a bug.
    """
    grimp = pytest.importorskip("grimp")
    graph = grimp.build_graph("oxn", include_external_packages=False)
    theirs = {
        (module, imported)
        for module in graph.modules
        for imported in graph.find_modules_directly_imported_by(module)
    }
    assert theirs, "grimp found no modules; oxn is not importable in this environment"

    ours = _oxn_module_edges(Path.cwd(), Path("src"), "oxn")
    with_type_only = _oxn_module_edges(Path.cwd(), Path("src"), "oxn", include_type_only=True)

    # Rule 1: grimp sees it, we see it only when type-only imports are included.
    unexplained_theirs = (theirs - ours) - with_type_only
    assert not unexplained_theirs, (
        f"grimp found edges that are not type-only imports: {sorted(unexplained_theirs)}"
    )

    # Rule 2: ours names a package whose submodule edge we also record.
    unexplained_ours = {
        (source, target)
        for source, target in ours - theirs
        if not _is_package_init_edge(source, target, ours)
    }
    assert not unexplained_ours, (
        f"OXN found edges grimp did not, that are not package-init edges: "
        f"{sorted(unexplained_ours)}"
    )

    # And the rules must not be doing all the work: the bulk has to actually agree.
    agreement = len(ours & theirs) / len(theirs)
    assert agreement > 0.75, f"only {agreement:.0%} of grimp's edges are shared"


def _is_package_init_edge(source: str, target: str, edges: set[tuple[str, str]]) -> bool:
    """Does `source` also import a submodule of `target`?

    That is what makes the edge to the package itself real rather than spurious:
    `from oxn import thresholds` runs `oxn/__init__.py` on the way to `oxn.thresholds`.
    """
    return any(other.startswith(f"{target}.") for owner, other in edges if owner == source)
