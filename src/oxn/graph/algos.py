"""Graph algorithms, implemented here rather than taken from networkx.

Not purity: ``import networkx`` costs a measured **146 ms**, most of the hook's 200 ms
budget, for perhaps five functions (ADR-0001). All of this is ~250 lines, and networkx
serves as the *test* oracle on random graphs instead.

Every routine is iterative. Python's default 1000-frame recursion limit is reached by real
dependency chains, and the resulting crash gives no clue what happened.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator, Mapping, Sequence  # noqa: TC003 - runtime alias
from typing import TypeVar

Node = TypeVar("Node")
#: Any adjacency mapping. Callers pass plain dicts; nothing here mutates the input.
Graph = Mapping[Node, Iterable[Node]]


def strongly_connected_components(graph: Graph[Node]) -> list[list[Node]]:
    """Tarjan's SCC algorithm (Tarjan, *SIAM J. Computing* 1(2), 1972), iteratively.

    Returns components in reverse topological order. A component of size > 1 -- or a single
    node with a self-loop -- is a dependency cycle.
    """
    index_of: dict[Node, int] = {}
    low: dict[Node, int] = {}
    on_stack: set[Node] = set()
    stack: list[Node] = []
    components: list[list[Node]] = []
    counter = 0

    for start in graph:
        if start in index_of:
            continue

        # Each frame is (node, iterator over successors). Explicit, so deep graphs are fine.
        work: list[tuple[Node, Iterator[Node]]] = [(start, iter(graph.get(start, ())))]
        index_of[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)

        while work:
            node, successors = work[-1]
            advanced = False
            for successor in successors:
                if successor not in index_of:
                    index_of[successor] = low[successor] = counter
                    counter += 1
                    stack.append(successor)
                    on_stack.add(successor)
                    work.append((successor, iter(graph.get(successor, ()))))
                    advanced = True
                    break
                if successor in on_stack:
                    low[node] = min(low[node], index_of[successor])
            if advanced:
                continue

            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                component: list[Node] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(component)

    return components


def cycles(graph: Graph[Node]) -> list[list[Node]]:
    """Only the components that are genuinely cyclic."""
    found = []
    for component in strongly_connected_components(graph):
        if len(component) > 1:
            found.append(sorted(component, key=str))
        elif component and component[0] in graph.get(component[0], ()):
            found.append(component)
    return found


def condensation(
    graph: Graph[Node],
) -> tuple[dict[Node, int], dict[int, list[int]], list[list[Node]]]:
    """Collapse each SCC to a single node, yielding a DAG.

    Returns ``(node -> component id, component DAG, components)``. Everything downstream --
    levelization, CCD, propagation cost -- runs on this, because those measures are only
    defined on an acyclic graph.
    """
    components = strongly_connected_components(graph)
    membership = {node: index for index, component in enumerate(components) for node in component}

    dag: dict[int, list[int]] = {index: [] for index in range(len(components))}
    for node, successors in graph.items():
        source = membership[node]
        for successor in successors:
            target = membership.get(successor)
            if target is not None and target != source and target not in dag[source]:
                dag[source].append(target)
    return membership, dag, components


def topological_order(dag: Graph[int]) -> list[int]:
    """Kahn's algorithm. Nodes in a cycle are omitted, which cannot happen on a condensation."""
    indegree: dict[int, int] = {node: 0 for node in dag}
    for successors in dag.values():
        for successor in successors:
            indegree[successor] = indegree.get(successor, 0) + 1

    queue = deque(sorted(node for node, degree in indegree.items() if degree == 0))
    order: list[int] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for successor in dag.get(node, ()):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                queue.append(successor)
    return order


def transitive_closure(dag: Graph[int], order: Sequence[int] | None = None) -> dict[int, int]:
    """Reflexive-transitive closure as a bitmask per node.

    Warshall is O(N^3) and hopeless in Python at a few thousand nodes. Walking the DAG in
    reverse topological order and OR-ing children's masks is O(E * N/64) using Python's
    arbitrary-precision integers as bitsets -- milliseconds where Warshall takes minutes.
    """
    sequence = list(order) if order is not None else topological_order(dag)
    reach: dict[int, int] = {}
    for node in reversed(sequence):
        mask = 1 << node
        for successor in dag.get(node, ()):
            mask |= reach.get(successor, 1 << successor)
        reach[node] = mask
    for node in dag:
        reach.setdefault(node, 1 << node)
    return reach


def levels(dag: Graph[int], order: Sequence[int] | None = None) -> dict[int, int]:
    """Lakos levelization: ``level(c) = 0`` with no dependencies, else ``1 + max(level(d))``."""
    sequence = list(order) if order is not None else topological_order(dag)
    depth: dict[int, int] = {}
    for node in reversed(sequence):
        successors = list(dag.get(node, ()))
        depth[node] = 1 + max((depth.get(s, 0) for s in successors), default=-1)
    for node in dag:
        depth.setdefault(node, 0)
    return depth


def shortest_path(graph: Graph[Node], source: Node, target: Node) -> list[Node] | None:
    """Breadth-first shortest path, for actionable diagnostics.

    "``domain`` depends on ``infrastructure``" is not actionable; "``domain.order`` imports
    ``app.repo`` imports ``infra.db``" is, because it names the edge to remove.
    """
    if source == target:
        return [source]
    previous: dict[Node, Node] = {}
    seen = {source}
    queue = deque([source])
    while queue:
        node = queue.popleft()
        for successor in graph.get(node, ()):
            if successor in seen:
                continue
            seen.add(successor)
            previous[successor] = node
            if successor == target:
                path = [target]
                while path[-1] != source:
                    path.append(previous[path[-1]])
                return list(reversed(path))
            queue.append(successor)
    return None


def modularity(graph: Graph[Node], partition: Mapping[Node, str]) -> float:
    """Newman-Girvan modularity of a *given* partition (Newman & Girvan, PRE 69, 026113).

    OXN measures the modularisation a project already declares -- its packages, directories
    or layers -- rather than discovering communities. The question is "does the declared
    structure match the actual dependency structure?", and that is a measurement, not a
    search.
    """
    degree: dict[Node, int] = {}
    edges = 0
    for node, successors in graph.items():
        for successor in successors:
            if successor not in graph:
                continue
            edges += 1
            degree[node] = degree.get(node, 0) + 1
            degree[successor] = degree.get(successor, 0) + 1

    if edges == 0:
        return 0.0

    total = 2 * edges
    score = 0.0
    for node, successors in graph.items():
        for successor in successors:
            if successor in graph and partition.get(node) == partition.get(successor):
                score += 1 - (degree.get(node, 0) * degree.get(successor, 0)) / total
    return score / total
