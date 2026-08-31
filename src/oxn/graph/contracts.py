"""Architecture conformance as a Reflexion Model.

Murphy, Notkin & Sullivan, "Software reflexion models" (*FSE 1995* / *TSE 2001*): compare a
declared high-level model against the model extracted from source, and classify each edge as

* **convergent** -- declared and present, the architecture working;
* **divergent** -- present but not declared, a violation;
* **absent** -- declared but never exercised, usually a stale rule.

That framing is strictly better than a flat list of forbidden imports, because it surfaces
rules that have quietly stopped meaning anything as well as code that has drifted.

Every violation carries the **shortest import chain** that produced it. "domain depends on
infrastructure" is not actionable; "domain/order.py -> app/repo.py -> infra/db.py" names the
edge to delete.

Contracts are passed in rather than read from configuration: `oxn.yaml` arrives in a later
phase, and nothing here should have to change when it does.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping, Sequence

    from oxn.graph.algos import Graph


@dataclass(frozen=True, slots=True)
class Layer:
    """A named set of paths, matched by glob."""

    name: str
    patterns: tuple[str, ...]

    def matches(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, pattern) for pattern in self.patterns)


@dataclass(frozen=True, slots=True)
class Violation:
    """One divergent edge, with the evidence that proves it."""

    contract: str
    source: str
    target: str
    #: The shortest concrete import chain realising the violation.
    chain: tuple[str, ...] = ()
    detail: str = ""

    def __str__(self) -> str:
        route = " -> ".join(self.chain) if self.chain else f"{self.source} -> {self.target}"
        return f"[{self.contract}] {route}" + (f": {self.detail}" if self.detail else "")


@dataclass
class Contract:
    """A rule the architecture is supposed to obey."""

    name: str
    kind: str
    #: For ``layered``: **outermost first**. A layer may depend on every layer *after* it and
    #: on none before it. So ``order=("infrastructure", "application", "domain")`` is the
    #: usual clean-architecture arrangement -- infrastructure may reach application and
    #: domain, application may reach domain, and domain may reach neither. The direction is
    #: easy to read backwards, which is why it is spelled out here.
    order: tuple[str, ...] = ()
    source: str | None = None
    forbidden: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()
    package: str | None = None
    allowed_entrypoints: tuple[str, ...] = ()


@dataclass
class ConformanceReport:
    """The result of checking a source model against a declared one."""

    convergent: int = 0
    divergent: list[Violation] = field(default_factory=list)
    absent: list[tuple[str, str]] = field(default_factory=list)
    unassigned: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.divergent

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "PASSED" if self.passed else "FAILED",
            "convergent": self.convergent,
            "divergent": [
                {
                    "contract": violation.contract,
                    "source": violation.source,
                    "target": violation.target,
                    "chain": list(violation.chain),
                    "detail": violation.detail,
                }
                for violation in self.divergent
            ],
            "absent": [{"source": s, "target": t} for s, t in self.absent],
            "unassigned_files": self.unassigned,
        }


def assign_layers(paths: Sequence[str], layers: Sequence[Layer]) -> dict[str, str | None]:
    """Map each file to its layer. First match wins, so order the layers most-specific first."""
    assignment: dict[str, str | None] = {}
    for path in paths:
        assignment[path] = next((layer.name for layer in layers if layer.matches(path)), None)
    return assignment


def check_contracts(
    file_graph: Graph[str],
    layers: Sequence[Layer],
    contracts: Sequence[Contract],
) -> ConformanceReport:
    """Check a file-level import graph against declared contracts."""
    paths = sorted(file_graph)
    assignment = assign_layers(paths, layers)
    report = ConformanceReport(
        unassigned=[path for path, layer in assignment.items() if layer is None]
    )
    layer_edges = _layer_edges(file_graph, paths, assignment)

    for contract in contracts:
        report.divergent.extend(_check_one(contract, file_graph, assignment, layer_edges))

    violating = {(violation.source, violation.target) for violation in report.divergent}
    report.convergent = sum(1 for edge in layer_edges if edge not in violating)

    declared = _declared_edges(contracts)
    if declared:
        report.absent = sorted(edge for edge in declared if edge not in layer_edges)
    return report


def _layer_edges(
    file_graph: Graph[str], paths: list[str], assignment: dict[str, str | None]
) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """Every concrete cross-layer edge, grouped by the layer pair it crosses.

    *Every* edge, not one exemplar per pair. Collapsing them was enough for a
    Reflexion-style report -- "does this layer reach that one" -- but not for a gate: a
    finding keyed on the layer pair forgives every later import between the same two
    layers, so the baseline could never see a new violation or notice one getting worse.
    """
    edges: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for source in paths:
        source_layer = assignment[source]
        if source_layer is None:
            continue
        for target in file_graph.get(source, ()):
            target_layer = assignment.get(target)
            if target_layer is not None and target_layer != source_layer:
                edges.setdefault((source_layer, target_layer), []).append((source, target))
    return edges


def _declared_edges(contracts: Sequence[Contract]) -> set[tuple[str, str]]:
    """Layer pairs a contract explicitly permits, which is what makes *absence* meaningful.

    An allowed dependency that never occurs is a Reflexion "absent" edge: the model says
    these layers talk and the code says they do not, which is worth reporting even though
    nothing is broken.
    """
    declared: set[tuple[str, str]] = set()
    for contract in contracts:
        if contract.kind == "layered":
            declared |= _layered_allowed(contract)
    return declared


def _layered_allowed(contract: Contract) -> set[tuple[str, str]]:
    """In a layered contract, each layer may depend on every layer below it."""
    return {
        (contract.order[higher], contract.order[lower])
        for higher in range(len(contract.order))
        for lower in range(higher + 1, len(contract.order))
    }


def _check_one(
    contract: Contract,
    file_graph: Graph[str],
    assignment: Mapping[str, str | None],
    layer_edges: Mapping[tuple[str, str], list[tuple[str, str]]],
) -> list[Violation]:
    if contract.kind == "layered":
        return _check_layered(contract, file_graph, layer_edges)
    if contract.kind == "forbidden":
        return _check_forbidden(contract, file_graph, layer_edges)
    if contract.kind == "independence":
        return _check_independence(contract, file_graph, layer_edges)
    if contract.kind == "deep_import":
        return _check_deep_import(contract, file_graph, assignment)
    return []


def _check_layered(
    contract: Contract,
    file_graph: Graph[str],
    layer_edges: Mapping[tuple[str, str], list[tuple[str, str]]],
) -> list[Violation]:
    """A lower layer must never depend on a higher one."""
    position = {name: index for index, name in enumerate(contract.order)}
    violations: list[Violation] = []
    for (source_layer, target_layer), edges in layer_edges.items():
        if source_layer not in position or target_layer not in position:
            continue
        if position[source_layer] <= position[target_layer]:
            continue
        violations.extend(
            Violation(
                contract=contract.name,
                source=source_layer,
                target=target_layer,
                chain=(source, target),
                detail=f"{source_layer} must not depend on {target_layer}",
            )
            for source, target in edges
        )
    return violations


def _check_forbidden(
    contract: Contract,
    file_graph: Graph[str],
    layer_edges: Mapping[tuple[str, str], list[tuple[str, str]]],
) -> list[Violation]:
    forbidden = set(contract.forbidden)
    return [
        Violation(
            contract=contract.name,
            source=source_layer,
            target=target_layer,
            chain=(source, target),
            detail=f"{source_layer} may not depend on {target_layer}",
        )
        for (source_layer, target_layer), edges in layer_edges.items()
        if source_layer == contract.source and target_layer in forbidden
        for source, target in edges
    ]


def _check_independence(
    contract: Contract,
    file_graph: Graph[str],
    layer_edges: Mapping[tuple[str, str], list[tuple[str, str]]],
) -> list[Violation]:
    """Named modules must not know about each other, in either direction."""
    named = set(contract.modules)
    return [
        Violation(
            contract=contract.name,
            source=source_layer,
            target=target_layer,
            chain=(source, target),
            detail=f"{source_layer} and {target_layer} must remain independent",
        )
        for (source_layer, target_layer), edges in layer_edges.items()
        if source_layer in named and target_layer in named
        for source, target in edges
    ]


def _check_deep_import(
    contract: Contract, file_graph: Graph[str], assignment: Mapping[str, str | None]
) -> list[Violation]:
    """A package may only be entered through its declared entry points."""
    detail = f"{contract.package} may only be entered via {', '.join(contract.allowed_entrypoints)}"
    return [
        Violation(
            contract=contract.name,
            source=source,
            target=target,
            chain=(source, target),
            detail=detail,
        )
        for source in sorted(file_graph)
        if assignment.get(source) != contract.package
        for target in file_graph.get(source, ())
        if _enters_illegally(target, contract, assignment)
    ]


def _enters_illegally(
    target: str, contract: Contract, assignment: Mapping[str, str | None]
) -> bool:
    """Does this edge reach into the package somewhere other than a declared entry point?"""
    if assignment.get(target) != contract.package:
        return False
    return not any(fnmatch.fnmatch(target, pattern) for pattern in contract.allowed_entrypoints)
