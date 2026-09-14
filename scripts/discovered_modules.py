"""Discovered modules against declared ones, as an architecture-erosion signal (P12).

    python scripts/discovered_modules.py                    # OXN itself
    python scripts/discovered_modules.py benchmarks/corpora/typescript-nest

**The scoping decision this obeys is `docs/metrics.md` section 4.6, not a preference.** OXN
computes Newman-Girvan Q *on the partition a project already declares* -- its directories or
its `oxn.yaml` layers -- because that is a measurement, exact and O(E), and it belongs on the
hook path. Community *detection* is a search, and the same section adopts networkx's Louvain
"in the research/eval harness only, where it is not on the hook path". So this lives in
`scripts/` and imports networkx; nothing under `src/` does.

**What the comparison means.** Q(declared) says whether the structure a project claims matches
the dependencies it has. Louvain finds the partition that maximises Q without being told the
claim. The gap between them is the interesting number: a project whose declared modules *are*
its dependency communities has little gap, and a large one says the code has grown a structure
its directories no longer describe -- which is architecture erosion stated as a measurement
rather than as an opinion.

**It is a signal and not a verdict, and the direction matters.** Louvain optimises Q and will
therefore always find a partition at least as good as the declared one; a gap is not by itself
evidence of anything wrong. Louvain is also non-deterministic without a seed and its resolution
parameter decides how many communities exist at all. Both are pinned here so two runs of this
script agree, which is the least a signal has to do before anyone argues from it.

**Measured 2026-09-14, and the raw gap does not discriminate.** Four projects, this script's
pinned settings:

==================  =====  ========  ==========  ===========  =============  =======
project             files  declared  discovered  Q declared   Q discovered   gap
==================  =====  ========  ==========  ===========  =============  =======
`oxn`                 191        13          29      +0.0875        +0.2878  +0.2003
`go-kit`              256        57          54      +0.0965        +0.3898  +0.2933
`typescript-nest`   1,911       600         147      +0.1106        +0.3874  +0.2767
`python-httpx`         60         5           7      +0.1289        +0.2351  +0.1062
==================  =====  ========  ==========  ===========  =============  =======

Every one shows a large gap, and OXN's is the *smallest* of the three larger projects -- which
is not a result anyone should be pleased by, because it means the number is not measuring what
"erosion" is supposed to mean. Louvain maximises Q by construction and a directory tree is not
trying to; the gap is dominated by that difference rather than by how well any of these projects
is organised. Q(declared) itself separates them barely at all: +0.0875 to +0.1289 across an
order of magnitude of size and four languages.

So the honest form of the P12 claim is **not** "gap = erosion". What a gap could support is a
comparison of one project against *itself over time*, where the confound is held fixed, or
against a null model with the same degree sequence. Neither is built here, and saying so is
worth more than a metric that ranks four healthy codebases by an artefact of the method.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

#: Pinned so the answer is reproducible. Louvain is randomised, and an erosion signal that
#: moves between runs is not one.
SEED = 20260914
#: Louvain's resolution. 1.0 is the paper's default; higher finds more, smaller communities.
RESOLUTION = 1.0


def declared_partition(membership: dict[str, str]) -> dict[str, str]:
    """What the project says its modules are: `DependencyGraph.membership`, file -> component."""
    return dict(membership)


def discovered_partition(graph: dict[str, set[str]]) -> dict[str, str]:
    """What Louvain finds, with the graph undirected because modularity is defined on one.

    Direction is real and this throws it away, which is the method's limitation rather than an
    oversight: Newman-Girvan Q has no directed form that these tools implement, and
    `oxn.graph.algos.modularity` makes the same choice for the declared partition -- so the two
    numbers being compared are at least computed the same way.
    """
    import networkx as nx

    undirected = nx.Graph()
    undirected.add_nodes_from(graph)
    for source, targets in graph.items():
        for target in targets:
            if source != target:
                undirected.add_edge(source, target)

    communities = nx.community.louvain_communities(undirected, seed=SEED, resolution=RESOLUTION)
    return {node: f"c{index}" for index, community in enumerate(communities) for node in community}


def disagreements(declared: dict[str, str], discovered: dict[str, str]) -> list[tuple[str, int]]:
    """Declared components whose files Louvain scatters, worst first.

    Counted as "how many discovered communities does this declared component's code fall into":
    one means the directory and the dependency structure agree about it, and several means the
    files sharing a directory do not share a neighbourhood.
    """
    scatter: dict[str, set[str]] = {}
    for path, component in declared.items():
        found = discovered.get(path)
        if found is not None:
            scatter.setdefault(component, set()).add(found)
    return sorted(
        ((component, len(found)) for component, found in scatter.items()),
        key=lambda row: (-row[1], row[0]),
    )


def main(target: Path) -> int:
    from oxn.config import Config
    from oxn.graph.algos import modularity
    from oxn.graph.depgraph import build_dependency_graph
    from oxn.graph.indexer import Indexer

    # `exclude` from the project's own `oxn.yaml`, or this measures whatever happens to be
    # under the root -- on OXN that is 1,464 files of fetched benchmark corpora, and the
    # answer becomes "other people's code has a different module structure from ours". The
    # same defect `oxn index` had, which is why `check._indexer` builds one this way.
    settings = Config.load(target)
    with Indexer(
        root=settings.root,
        cache_path=settings.root / ".oxn/cache/graph.db",
        exclude=settings.exclude,
        measure=False,
    ) as indexer:
        graph = build_dependency_graph(indexer.root, indexer.sources([settings.root]))

    files = {path: set(targets) for path, targets in graph.files.items()}
    if not files:
        print(f"{target}: no import graph")
        return 1

    declared = declared_partition(graph.membership)
    discovered = discovered_partition(files)
    q_declared = modularity(files, declared)
    q_discovered = modularity(files, discovered)

    print(f"{target}")
    print(f"  files              {len(files):,}")
    print(f"  declared modules   {len(set(declared.values())):,}")
    print(f"  discovered         {len(set(discovered.values())):,}  (Louvain, seed {SEED})")
    print(f"  Q declared         {q_declared:+.4f}")
    print(f"  Q discovered       {q_discovered:+.4f}")
    print(f"  gap                {q_discovered - q_declared:+.4f}")

    scattered = [row for row in disagreements(declared, discovered) if row[1] > 1]
    print(f"  scattered modules  {len(scattered)} of {len(set(declared.values()))}")
    for component, count in scattered[:8]:
        print(f"      {component:40} across {count} communities")

    sizes = Counter(discovered.values())
    print(f"  largest community  {max(sizes.values()):,} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()))
