"""Every dependency ring OXN can find, and the smallest edit that would break it (P12).

    python scripts/measure_cycles.py                    # OXN itself
    python scripts/measure_cycles.py benchmarks/corpora/typescript-nest ...

**This is the measurement that decided `oxn[asp]`.** P12 asked for clingo repair suggestions
-- "the minimal set of edges to remove to break all cycles" -- and the project's rule is to
hand-roll first and adopt a dependency only against numbers. So the rings were counted before
anything was built, and they are small: a Held-Karp dynamic program over node subsets
(`graph.algos.minimum_feedback_arcs`, ~40 lines, no dependency) decides a ring of 20
components in under a second and answers 19 of the 21 found here. clingo earns its place on
the other two, and nothing else.

**Measured 2026-09-14**, directory granularity, initialisation edges only. The last four
columns describe each project's *largest* ring, which is the one that decides the method:

=======================  =====  =====  ==========  ==========  ========  ========
project                  files  rings  nodes       edges       break     method
=======================  =====  =====  ==========  ==========  ========  ========
`oxn`                      195      1           2           2         1  exact
`python-httpx`              60      1           2           2         1  exact
`java-spring-petclinic`     50      0           -           -         -  -
`javascript-eslint`      1,487      1           3           4         1  exact
`go-kit`                   256      3           3           5         2  exact
`rust-ripgrep`             110      3           4           6         3  exact
`typescript-nest`        1,913     12          25          86        19  solver
=======================  =====  =====  ==========  ==========  ========  ========

`typescript-nest` is the whole reason the solver exists: 12 rings, of which its 25-component
and 24-component ones (86 and 105 edges) are the only two of the 21 found here that the exact
program declines. Their minima are 19 and 27 edges. Every other project's worst ring is four
components or fewer and is decided exactly, with no dependency, in microseconds.

Run it to regenerate the per-ring detail; the summary above is what the table collapses.

**The two methods were cross-checked against each other**, which is the only reason the
solver's two answers can be believed: on every ring the dynamic program decides, clingo
proves the identical minimum (`tests/test_graph_rings.py`). Both were also wrong in the same
way at first -- neither can cut a self-loop, because no ordering places a node after itself
-- and each found that in the other.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _graph(project: Path):  # noqa: ANN202 - a script, and the type is an implementation detail
    """The component graph `oxn arch` would build for ``project``.

    Runs from inside the project so `oxn.yaml`'s `exclude` is the project's own. Building an
    indexer without it is how `scripts/discovered_modules.py` once measured 5,748 files of a
    1,913-file tree, and there is no reason for a second script to rediscover that.
    """
    from oxn.graph.depgraph import build_dependency_graph
    from oxn.report import _GRANULARITIES, _indexer

    here = Path.cwd()
    os.chdir(project)
    try:
        with _indexer() as indexer:
            indexer.index([Path(".")])
            files = indexer.sources([Path(".")])
            graph = build_dependency_graph(
                indexer.root, files, component_of=_GRANULARITIES["directory"]
            )
    finally:
        os.chdir(here)
    return graph, len(files)


def _report(project: Path) -> None:
    from oxn.graph.algos import EXACT_CUT_LIMIT
    from oxn.graph.rings import rings_and_cuts

    graph, files = _graph(project)
    rings, cuts = rings_and_cuts(graph.hard_components)
    print(f"\n{project.name}  ({files:,} files, {len(rings)} rings)")
    if not rings:
        print("  no dependency cycles")
        return
    for ring, cut in sorted(zip(rings, cuts, strict=True), key=lambda pair: -len(pair[0])):
        edges = _edges_inside(graph.hard_components, ring)
        how = "exact" if len(ring) <= EXACT_CUT_LIMIT else "solver"
        size = "declined" if cut is None else str(len(cut))
        print(f"  nodes={len(ring):3}  edges={edges:4}  break={size:>8}  [{how}]")
        for member in ring[:3]:
            print(f"      {member}")


def _edges_inside(components: dict[str, set[str]], ring: list[str]) -> int:
    """How many edges the ring has among its own members.

    The size the search actually faces. A component in a 25-member ring also imports plenty
    of things outside it, and counting those would describe the neighbourhood rather than the
    problem.
    """
    members = set(ring)
    return sum(1 for node in ring for target in components.get(node, ()) if target in members)


def main(argv: list[str]) -> int:
    projects = [Path(raw).resolve() for raw in argv] or [ROOT]
    for project in projects:
        _report(project)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
