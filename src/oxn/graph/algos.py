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
from dataclasses import dataclass, field
from typing import Generic, TypeVar

Node = TypeVar("Node")
#: Any adjacency mapping. Callers pass plain dicts; nothing here mutates the input.
Graph = Mapping[Node, Iterable[Node]]


def strongly_connected_components(graph: Graph[Node]) -> list[list[Node]]:
    """Tarjan's SCC algorithm (Tarjan, *SIAM J. Computing* 1(2), 1972), iteratively.

    Returns components in reverse topological order. A component of size > 1 -- or a single
    node with a self-loop -- is a dependency cycle.
    """
    return _Tarjan(graph).run()


@dataclass
class _Tarjan(Generic[Node]):
    """Tarjan's algorithm and the six pieces of state it threads.

    A class rather than a function because the state is the algorithm: `index_of`, `low`,
    the stack and the on-stack set are mutated by every step, and passing six mutable
    structures between free functions would say less about the algorithm than this does.
    Iterative throughout -- Python's 1000-frame recursion limit is reached by real
    dependency chains, and the resulting crash gives no clue what happened.
    """

    graph: Graph[Node]
    index_of: dict[Node, int] = field(default_factory=dict)
    low: dict[Node, int] = field(default_factory=dict)
    on_stack: set[Node] = field(default_factory=set)
    stack: list[Node] = field(default_factory=list)
    components: list[list[Node]] = field(default_factory=list)
    counter: int = 0

    def run(self) -> list[list[Node]]:
        for start in self.graph:
            if start not in self.index_of:
                self._visit(start)
        return self.components

    def _visit(self, start: Node) -> None:
        """Depth-first from one root, with the call stack made explicit.

        Each frame is (node, iterator over successors), so a frame resumes where it left
        off rather than restarting the successor scan.
        """
        self._discover(start)
        work: list[tuple[Node, Iterator[Node]]] = [(start, self._successors(start))]

        while work:
            node, successors = work[-1]
            pushed = self._advance(node, successors)
            if pushed is not None:
                work.append((pushed, self._successors(pushed)))
                continue

            work.pop()
            if work:
                parent = work[-1][0]
                self.low[parent] = min(self.low[parent], self.low[node])
            if self.low[node] == self.index_of[node]:
                self._close(node)

    def _advance(self, node: Node, successors: Iterator[Node]) -> Node | None:
        """Consume successors until one needs visiting; return it, or None when exhausted.

        Returning the node rather than pushing the frame here keeps the work stack owned by
        `_visit` alone.
        """
        for successor in successors:
            if successor not in self.index_of:
                self._discover(successor)
                return successor
            if successor in self.on_stack:
                self.low[node] = min(self.low[node], self.index_of[successor])
        return None

    def _discover(self, node: Node) -> None:
        """First visit: number it, and put it on the component stack."""
        self.index_of[node] = self.low[node] = self.counter
        self.counter += 1
        self.stack.append(node)
        self.on_stack.add(node)

    def _close(self, root: Node) -> None:
        """`root` is a component root: everything above it on the stack is its component."""
        component: list[Node] = []
        while True:
            member = self.stack.pop()
            self.on_stack.discard(member)
            component.append(member)
            if member == root:
                break
        self.components.append(component)

    def _successors(self, node: Node) -> Iterator[Node]:
        return iter(self.graph.get(node, ()))


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
                return _trace_back(previous, source, target)
            queue.append(successor)
    return None


def _trace_back(previous: dict[Node, Node], source: Node, target: Node) -> list[Node]:
    """Walk the predecessor chain back to the source and hand it over forwards."""
    path = [target]
    while path[-1] != source:
        path.append(previous[path[-1]])
    return list(reversed(path))


def modularity(graph: Graph[Node], partition: Mapping[Node, str]) -> float:
    """Newman-Girvan modularity of a *given* partition (Newman & Girvan, PRE 69, 026113).

    OXN measures the modularisation a project already declares -- its packages, directories
    or layers -- rather than discovering communities. The question is "does the declared
    structure match the actual dependency structure?", and that is a measurement, not a
    search.
    """
    degree, edges = _degrees(graph)
    if edges == 0:
        return 0.0

    total = 2 * edges
    score = sum(
        1 - (degree.get(node, 0) * degree.get(successor, 0)) / total
        for node, successor in _internal_edges(graph)
        if partition.get(node) == partition.get(successor)
    )
    return score / total


def _degrees(graph: Graph[Node]) -> tuple[dict[Node, int], int]:
    """Undirected degree of every node, and the edge count, over in-tree edges only.

    An edge leaving the graph is not a dependency *within* the modularisation being
    measured, so counting it would compare the project against a structure it does not have.
    """
    degree: dict[Node, int] = {}
    edges = 0
    for node, successor in _internal_edges(graph):
        edges += 1
        degree[node] = degree.get(node, 0) + 1
        degree[successor] = degree.get(successor, 0) + 1
    return degree, edges


def _internal_edges(graph: Graph[Node]) -> Iterator[tuple[Node, Node]]:
    """Every edge whose target is also in the graph."""
    for node, successors in graph.items():
        for successor in successors:
            if successor in graph:
                yield node, successor


#: Components in one ring above which the exact cut is not attempted.
#:
#: The exact algorithm is exponential in the ring's *node* count, so this is a wall-clock
#: budget expressed in the only parameter that matters. Measured on this machine against
#: `typescript-nest`'s largest ring, induced down to each size: 13 nodes 0.005 s, 16 nodes
#: 0.049 s, 18 nodes 0.224 s, **20 nodes 0.99 s**, 21 nodes 2.0 s, 22 nodes 4.3 s. Twenty is
#: the last size under a second, and this is a report-path call, never the hook's.
#:
#: Over seven projects (`scripts/measure_cycles.py`) that covers 19 of 21 rings. The two it
#: does not are both in `typescript-nest`, at 24 and 25 components.
EXACT_CUT_LIMIT = 20


def minimum_feedback_arcs(
    graph: Graph[Node], *, limit: int = EXACT_CUT_LIMIT
) -> tuple[tuple[Node, Node], ...] | None:
    """The smallest set of edges whose removal leaves ``graph`` acyclic.

    The minimum feedback arc set, exactly -- not a greedy approximation. Removing a cycle is
    the one architectural repair a tool can state as a concrete edit, and an answer that is
    merely *a* set of edges is worth much less than the smallest one: it is the difference
    between "delete these two imports" and "delete some of these twelve".

    Exact because the problem is small here, not because it is easy: minimum feedback arc set
    is NP-hard (Karp 1972), but a ring of ``n`` components admits a Held-Karp dynamic program
    over subsets -- ``dp[S | {v}] = min(dp[S] + |{u in S : v -> u}|)`` -- which is
    ``O(2^n * n)`` and decides ``n <= 20`` in under a second. Cutting the fewest edges is the
    same problem as ordering the components so the fewest edges point backwards, because any
    acyclic graph has a topological order and every edge against that order must go.

    Returns ``None`` -- never a guess -- when the ring is larger than ``limit``. An empty
    tuple means the graph was already acyclic; the two are not the same answer and a caller
    that conflates them reports "nothing to cut" for the hardest case it has.

    **A minimum set is not always the only one.** Two components joined by one edge each way
    are broken by cutting either, and both answers are equally minimal. The *size* returned
    here is proven; the particular edges are the lexicographically first such set, and a
    caller showing them to a person should say so rather than implying the choice was forced.
    """
    nodes = sorted(graph, key=str)
    if len(nodes) > limit:
        return None
    index = {node: position for position, node in enumerate(nodes)}
    # `back[v]` is the set of `u` with an edge `v -> u`: placing `v` after `u` makes it
    # backward. Bitmasks because the inner loop of the DP is "how many of these are already
    # placed", which `int.bit_count()` answers in one operation over the whole set.
    #
    # **A self-loop is cut unconditionally and kept out of `back`.** No ordering can put a
    # node after itself, so the dynamic program cannot see one, and an implementation that
    # leaves them to it returns a cut that does not break the cycle -- silently, because the
    # arithmetic is still self-consistent. `cycles` counts a self-loop as a ring, so this is
    # a case the caller genuinely reaches.
    cut: list[tuple[Node, Node]] = []
    back = [0] * len(nodes)
    for node in nodes:
        for target in graph[node]:
            if target == node:
                cut.append((node, node))
            elif target in index:
                back[index[node]] |= 1 << index[target]

    placed = 0
    for position in _best_order(back, len(nodes)):
        backward = back[position] & placed
        while backward:
            lowest = backward & -backward
            cut.append((nodes[position], nodes[lowest.bit_length() - 1]))
            backward ^= lowest
        placed |= 1 << position
    return tuple(cut)


def _best_order(back: Sequence[int], count: int) -> list[int]:
    """Held-Karp over subsets: the placement order with the fewest backward edges.

    ``dp[S]`` is the fewest backward edges achievable with exactly the nodes in ``S`` placed,
    in any order; the node added last is recorded so the winning order can be replayed. Ties
    go to the lowest-numbered node, which is what makes the result reproducible.
    """
    size = 1 << count
    unreachable = count * count + 1
    dp = [unreachable] * size
    dp[0] = 0
    last = [0] * size
    for subset in range(size):
        base = dp[subset]
        if base >= unreachable:
            continue
        remaining = (size - 1) & ~subset
        while remaining:
            lowest = remaining & -remaining
            node = lowest.bit_length() - 1
            cost = base + (back[node] & subset).bit_count()
            if cost < dp[subset | lowest]:
                dp[subset | lowest] = cost
                last[subset | lowest] = node
            remaining ^= lowest

    order = []
    subset = size - 1
    while subset:
        node = last[subset]
        order.append(node)
        subset ^= 1 << node
    order.reverse()
    return order
