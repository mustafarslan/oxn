"""Metrics over the call graph: fan-in, fan-out, recursion cycles and dead code.

Henry & Kafura (*IEEE TSE* SE-7(5), 1981) defined fan-in and fan-out via *information flow*
-- parameters, globals, return values -- not merely calls. The near-universal
"count the calls" reading is a simplification, criticised by Shepperd (1988). OXN uses the
call-graph reading and says so rather than implying otherwise.

**Only confident edges participate.** A low-confidence guess would invent a call cycle, and
a phantom cycle inflates every cognitive-complexity score in it -- corrupting the metric
this project measures most carefully. Edges from L2, or from L1 where the target was
certain, are the only ones admitted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from oxn.graph.algos import cycles

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable, Mapping


@dataclass
class CallGraph:
    """A resolved call graph over entity ids."""

    edges: dict[str, set[str]] = field(default_factory=dict)
    #: Entities named as call targets that could not be resolved inside the tree.
    unresolved_targets: int = 0

    def add(self, source: str, target: str) -> None:
        self.edges.setdefault(source, set()).add(target)
        self.edges.setdefault(target, set())

    def fan_out(self, entity_id: str) -> int:
        return len(self.edges.get(entity_id, ()))

    def fan_in(self, entity_id: str) -> int:
        return sum(1 for targets in self.edges.values() if entity_id in targets)

    def recursion_cycles(self) -> list[list[str]]:
        """Every set of entities that can reach itself -- direct or mutual recursion.

        The Cognitive Complexity specification increments for "each method in a recursion
        cycle, whether direct or indirect", so this is what upgrades that increment from the
        direct-only approximation.
        """
        return cycles(self.edges)

    def in_recursion(self) -> set[str]:
        return {member for cycle in self.recursion_cycles() for member in cycle}


def build_call_graph(
    edges: Iterable[tuple[str, str | None, str, float]],
) -> CallGraph:
    """Assemble a call graph from ``(source, target, resolution, confidence)`` rows.

    Rows whose target is unknown, or whose confidence is below certainty at L0/L1, are
    counted rather than admitted -- see the module docstring for why.
    """
    graph = CallGraph()
    for source, target, resolution, confidence in edges:
        if target is None:
            graph.unresolved_targets += 1
            continue
        if resolution != "L2" and confidence < 1.0:
            graph.unresolved_targets += 1
            continue
        graph.add(source, target)
    return graph


@dataclass(frozen=True, slots=True)
class DeadCodeCandidate:
    """An entity nothing reaches from a declared root.

    Deliberately a *candidate*. Reflection, dependency injection, framework entry points and
    dynamic dispatch all make false positives inevitable, so this never blocks -- a gate
    that deletes code on this signal is dangerous.
    """

    entity_id: str
    qualified_name: str
    file_path: str
    kind: str


def unreachable(
    graph: CallGraph,
    entities: Mapping[str, tuple[str, str, str]],
    roots: Iterable[str],
) -> list[DeadCodeCandidate]:
    """Entities not reachable from ``roots``.

    ``entities`` maps id to ``(qualified name, file path, kind)``.
    """
    reached: set[str] = set()
    frontier = [root for root in roots if root in entities]
    while frontier:
        current = frontier.pop()
        if current in reached:
            continue
        reached.add(current)
        frontier.extend(graph.edges.get(current, ()))

    return sorted(
        (
            DeadCodeCandidate(entity_id, name, path, kind)
            for entity_id, (name, path, kind) in entities.items()
            if entity_id not in reached
        ),
        key=lambda candidate: (candidate.file_path, candidate.qualified_name),
    )


def default_roots(
    entities: Mapping[str, tuple[str, str, str]], *, test_prefixes: tuple[str, ...] = ("test_",)
) -> set[str]:
    """Entry points every project has, before any configured ones.

    Public API, ``main``, and test functions -- a test is a root because a test runner calls
    it, and treating tests as dead code would bury the report in noise.
    """
    roots: set[str] = set()
    for entity_id, (qualified_name, path, kind) in entities.items():
        leaf = qualified_name.rsplit(".", 1)[-1]
        if (
            leaf in {"main", "__main__"}
            or leaf.startswith(test_prefixes)
            or "/test" in path
            or path.startswith("test")
            or kind in {"class", "interface"}
            and not leaf.startswith("_")
        ):
            roots.add(entity_id)
        elif not leaf.startswith("_") and "." not in qualified_name.rsplit(".", 2)[-2:][0]:
            # A public module-level definition is reachable from outside the tree.
            roots.add(entity_id)
    return roots
