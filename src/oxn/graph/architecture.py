"""Tier-2 architecture metrics over the component dependency graph.

Every tool that does this well is commercial -- Sonargraph, Structure101, NDepend, Lattix,
CAST -- so ADR-0001's cost rule forces OXN to implement them. The algorithms are small once
the graph exists; the judgement is in the thresholds, and every one of those is sourced.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from oxn.graph.algos import condensation, cycles, levels, topological_order, transitive_closure
from oxn.thresholds import GOD_COMPONENT_MIN_LOC

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator, Mapping

    from oxn.graph.algos import Graph


@dataclass(frozen=True, slots=True)
class MartinMetrics:
    """Robert C. Martin, *Agile Software Development* (2002), ch. 20.

    ``I`` and ``A`` are exact given the graph. ``A`` is exact *syntactically* and
    approximate conceptually: Python duck typing lets a concrete class serve as an
    interface, and Go's implicit satisfaction means a struct can be the abstraction at a
    call site. Documented rather than hidden.
    """

    component: str
    afferent: int
    efferent: int
    abstract_types: int
    total_types: int

    @property
    def instability(self) -> float:
        """``I = Ce / (Ca + Ce)``. Zero when nothing depends on it and it depends on nothing."""
        total = self.afferent + self.efferent
        return self.efferent / total if total else 0.0

    @property
    def abstractness(self) -> float | None:
        """``A = Na / Nc``. ``None`` when the component declares no types at all.

        Very common in Go, Rust and functional Python. Reporting 0.0 there would place the
        component at the "concrete" end of the main sequence purely because it has no
        classes, which is a false signal -- so it is excluded from distance-based gates.
        """
        return self.abstract_types / self.total_types if self.total_types else None

    @property
    def distance(self) -> float | None:
        """``D = |A + I - 1|``: how far from Martin's main sequence.

        Martin's original divides by sqrt(2); the normalised form is what every tool uses,
        so OXN emits that and says so.
        """
        abstractness = self.abstractness
        return abs(abstractness + self.instability - 1) if abstractness is not None else None


@dataclass(frozen=True, slots=True)
class LakosMetrics:
    """John Lakos, *Large-Scale C++ Software Design* (1996), §4."""

    ccd: int
    component_count: int

    @property
    def acd(self) -> float:
        """Average component dependency."""
        return self.ccd / self.component_count if self.component_count else 0.0

    @property
    def ccd_balanced_binary_tree(self) -> float:
        """CCD of a balanced binary tree of the same size: ``(N+1)log2(N+1) - N``."""
        n = self.component_count
        return (n + 1) * math.log2(n + 1) - n if n else 0.0

    @property
    def nccd(self) -> float:
        """``NCCD > 1`` means more coupled than a balanced binary tree; ``>> 1`` means cycles."""
        baseline = self.ccd_balanced_binary_tree
        return self.ccd / baseline if baseline else 0.0


@dataclass(frozen=True, slots=True)
class Smell:
    """One architectural smell, with the evidence that produced it."""

    kind: str
    component: str
    severity: float
    detail: str
    members: tuple[str, ...] = ()


@dataclass
class ArchitectureReport:
    """Everything Tier 2 says about a component graph."""

    components: tuple[str, ...] = ()
    martin: dict[str, MartinMetrics] = field(default_factory=dict)
    lakos: LakosMetrics | None = None
    propagation_cost: float = 0.0
    modularity: float = 0.0
    cycles: list[list[str]] = field(default_factory=list)
    smells: list[Smell] = field(default_factory=list)
    levels: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "components": len(self.components),
            "cycles": [{"size": len(cycle), "members": cycle} for cycle in self.cycles],
            "propagation_cost": round(self.propagation_cost, 4),
            "modularity": round(self.modularity, 4),
            "lakos": None
            if self.lakos is None
            else {
                "ccd": self.lakos.ccd,
                "acd": round(self.lakos.acd, 2),
                "nccd": round(self.lakos.nccd, 3),
            },
            "smells": [
                {
                    "kind": smell.kind,
                    "component": smell.component,
                    "severity": round(smell.severity, 3),
                    "detail": smell.detail,
                }
                for smell in self.smells
            ],
        }


def analyse(
    graph: Graph[str],
    *,
    types: Mapping[str, tuple[int, int]] | None = None,
    sizes: Mapping[str, int] | None = None,
    partition: Mapping[str, str] | None = None,
    initialisation: Graph[str] | None = None,
) -> ArchitectureReport:
    """Compute every Tier-2 metric for a component graph.

    ``types`` maps a component to ``(abstract, total)`` type counts; ``sizes`` to lines of
    code. Both are optional: without them the structural metrics still hold, and the metrics
    that need them are simply omitted rather than faked.

    ``initialisation`` is the same graph without the imports that Python and CommonJS defer
    to call time, and **cycles are read from it** when it is given. Everything else here is
    about coupling -- what a component must know -- where a deferred import counts exactly
    like any other. A cycle is about *order*, and a function-body import is how both
    languages break one, so counting it manufactures the cycle it resolved: OXN's own tree
    reads as one 9-component ring on all imports and a 2-component one on these.
    """
    components = sorted(graph)
    report = ArchitectureReport(components=tuple(components))
    if not components:
        return report

    report.cycles = [sorted(cycle) for cycle in cycles(initialisation or graph)]
    report.martin = _martin(graph, types or {})

    membership, dag, groups = condensation(graph)
    order = topological_order(dag)
    reach = transitive_closure(dag, order)

    component_levels = levels(dag, order)
    report.levels = {name: component_levels.get(membership[name], 0) for name in components}

    # CCD sums the *reflexive* transitive closure: a component depends on itself.
    ccd = 0
    reachable_counts: dict[str, int] = {}
    for name in components:
        mask = reach.get(membership[name], 0)
        count = sum(len(groups[index]) for index in _bits(mask))
        reachable_counts[name] = count
        ccd += count
    report.lakos = LakosMetrics(ccd=ccd, component_count=len(components))

    # Propagation cost (MacCormack, Rusnak & Baldwin, *Management Science* 52(7), 2006):
    # the density of the visibility matrix.
    total = len(components)
    report.propagation_cost = sum(reachable_counts.values()) / (total * total)

    if partition:
        from oxn.graph.algos import modularity

        report.modularity = modularity(graph, partition)

    report.smells = detect_smells(graph, report, sizes or {})
    return report


def _bits(mask: int) -> list[int]:
    """Indices of the set bits in a bitmask."""
    out: list[int] = []
    index = 0
    while mask:
        if mask & 1:
            out.append(index)
        mask >>= 1
        index += 1
    return out


def _martin(graph: Graph[str], types: Mapping[str, tuple[int, int]]) -> dict[str, MartinMetrics]:
    """Afferent and efferent coupling per component, plus the abstractness they pair with."""
    afferent = _afferent(graph)
    return {
        name: MartinMetrics(
            component=name,
            afferent=afferent[name],
            efferent=len(set(_depends_on(graph, name))),
            abstract_types=types.get(name, (0, 0))[0],
            total_types=types.get(name, (0, 0))[1],
        )
        for name in graph
    }


def _afferent(graph: Graph[str]) -> dict[str, int]:
    """How many *other* components depend on each one.

    A self-edge is excluded on both sides: a component depending on itself says nothing
    about its stability, and counting it would make every package look more depended-upon
    than it is.
    """
    counts = dict.fromkeys(graph, 0)
    for name in graph:
        for successor in _depends_on(graph, name):
            counts[successor] += 1
    return counts


def _depends_on(graph: Graph[str], name: str) -> Iterator[str]:
    """The in-tree components `name` depends on, itself excluded."""
    return (target for target in graph[name] if target in graph and target != name)


#: Below this many components a percentile is not a percentile -- with three sizes the p90
#: *is* the maximum, so nothing could ever exceed it. Small systems use the fixed floor.
_MIN_COMPONENTS_FOR_PERCENTILE = 10


def _god_component_cutoff(sizes: Mapping[str, int]) -> int:
    """Size above which a component is a God Component.

    Arcan derives this adaptively from the system plus a benchmark corpus. OXN uses the
    project's own p90 where there are enough components for that to mean something, and
    never goes below the published floor.
    """
    if len(sizes) < _MIN_COMPONENTS_FOR_PERCENTILE:
        return GOD_COMPONENT_MIN_LOC
    ordered = sorted(sizes.values())
    percentile = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
    return max(percentile, GOD_COMPONENT_MIN_LOC)


def detect_smells(
    graph: Graph[str], report: ArchitectureReport, sizes: Mapping[str, int]
) -> list[Smell]:
    """Arcan's four architectural smells (Fontana et al., *ICSA-C 2017*).

    Arcan itself is now commercial, so the cost rule forces reimplementation. Thresholds
    follow the literature where it states one and are otherwise OXN's own, marked as such --
    Arcan's own defaults are *system-adaptive*, derived from percentile analysis over the
    system under study plus a benchmark corpus.

    One function per smell, because that is how the paper is organised and how anyone
    checking this against it will read: four independent detectors over a shared graph.
    """
    smells = [
        *_cyclic_dependency(report),
        *_hub_like_dependency(graph, report),
        *_unstable_dependency(graph, report),
        *_god_component(sizes),
    ]
    # `component` breaks ties, and it is not cosmetic: without it the order of equally
    # severe smells fell out of set and dict iteration, so `oxn arch` produced a different
    # report on every run of the same code -- three runs of typescript-nest gave three
    # different outputs. A report that cannot be diffed against the previous commit cannot
    # show a trend, which is most of what a report is for.
    smells.sort(key=lambda smell: (smell.kind, -smell.severity, smell.component))
    return smells


def _cyclic_dependency(report: ArchitectureReport) -> list[Smell]:
    """Any strongly connected component larger than one."""
    return [
        Smell(
            kind="cyclic_dependency",
            component=cycle[0],
            severity=float(len(cycle)),
            detail=f"{len(cycle)} components form a dependency cycle",
            members=tuple(cycle),
        )
        for cycle in report.cycles
    ]


def _hub_like_dependency(graph: Graph[str], report: ArchitectureReport) -> list[Smell]:
    """High fan-in *and* fan-out, and balanced between them.

    Balance is what separates a hub from a component that is merely popular: something
    depended on by fifty things and depending on one is a shared library, not a tangle.
    """
    from oxn.thresholds import HUB_MIN_DEGREE, HUB_PERCENTILE

    degrees = {
        name: (report.martin[name].afferent, report.martin[name].efferent)
        for name in graph
        if name in report.martin
    }
    totals = sorted(afferent + efferent for afferent, efferent in degrees.values())
    if not totals:
        return []

    cutoff = totals[min(len(totals) - 1, int(len(totals) * HUB_PERCENTILE))]
    found = []
    for name, (afferent, efferent) in degrees.items():
        low, high = min(afferent, efferent), max(afferent, efferent)
        if (
            afferent + efferent > cutoff
            and low >= HUB_MIN_DEGREE
            and high > 0
            and low / high >= 0.5
        ):
            found.append(
                Smell(
                    kind="hub_like_dependency",
                    component=name,
                    severity=float(afferent + efferent),
                    detail=f"fan-in {afferent}, fan-out {efferent}",
                )
            )
    return found


def _unstable_dependency(graph: Graph[str], report: ArchitectureReport) -> list[Smell]:
    """A component depending on things less stable than itself -- Martin's SDP, violated."""
    found = [_sdp_violation(name, graph, report) for name in graph]
    return [smell for smell in found if smell is not None]


def _sdp_violation(name: str, graph: Graph[str], report: ArchitectureReport) -> Smell | None:
    """Is this one component depending downhill in stability?"""
    from oxn.thresholds import UNSTABLE_DEPENDENCY_RATIO

    metrics = report.martin.get(name)
    if metrics is None:
        return None
    dependencies = [other for other in graph[name] if other in report.martin and other != name]
    if not dependencies:
        return None

    worse = [
        other for other in dependencies if report.martin[other].instability > metrics.instability
    ]
    ratio = len(worse) / len(dependencies)
    if ratio < UNSTABLE_DEPENDENCY_RATIO:
        return None
    return Smell(
        kind="unstable_dependency",
        component=name,
        severity=ratio,
        detail=(
            f"{len(worse)} of {len(dependencies)} dependencies are less stable "
            f"(I={metrics.instability:.2f})"
        ),
        members=tuple(sorted(worse)),
    )


def _god_component(sizes: Mapping[str, int]) -> list[Smell]:
    """Excessively large, against a cutoff derived from this project plus a literature floor."""
    if not sizes:
        return []
    cutoff = _god_component_cutoff(sizes)
    return [
        Smell(
            kind="god_component",
            component=name,
            severity=float(size),
            detail=f"{size} lines, above the {cutoff}-line threshold",
        )
        for name, size in sizes.items()
        if size > cutoff
    ]
