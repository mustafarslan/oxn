"""Graph algorithms, against hand-computed answers and networkx.

networkx is a *test* oracle only. It costs a measured 146 ms to import, most of the hook's
budget, which is why these ~250 lines exist at all (ADR-0001). `tests/test_import_guard.py`
fails the build if it ever reaches runtime.
"""

from __future__ import annotations

import random

import pytest

from oxn.graph.algos import (
    condensation,
    cycles,
    levels,
    modularity,
    shortest_path,
    strongly_connected_components,
    topological_order,
    transitive_closure,
)

#: networkx comparisons are oracle-lane: the point of writing these algorithms by hand is
#: that networkx never becomes a runtime dependency.
requires_networkx = pytest.mark.oracle


def _networkx():
    return pytest.importorskip("networkx")


def random_graph(nodes: int, edges: int, seed: int) -> dict[int, list[int]]:
    rng = random.Random(seed)
    graph: dict[int, list[int]] = {node: [] for node in range(nodes)}
    for _ in range(edges):
        source, target = rng.randrange(nodes), rng.randrange(nodes)
        if target not in graph[source]:
            graph[source].append(target)
    return graph


# ---- hand-computed ---------------------------------------------------------------------


def test_finds_a_simple_cycle() -> None:
    graph = {"a": ["b"], "b": ["c"], "c": ["a"], "d": ["a"], "e": []}
    assert cycles(graph) == [["a", "b", "c"]]


def test_a_self_loop_is_a_cycle() -> None:
    assert cycles({"a": ["a"], "b": []}) == [["a"]]


def test_an_acyclic_graph_has_no_cycles() -> None:
    assert cycles({"a": ["b"], "b": ["c"], "c": []}) == []


def test_condensation_is_acyclic() -> None:
    graph = {"a": ["b"], "b": ["a"], "c": ["a"]}
    _, dag, components = condensation(graph)
    assert cycles(dag) == []
    assert sorted(len(component) for component in components) == [1, 2]


def test_levels_count_the_longest_dependency_chain() -> None:
    dag = {0: [1], 1: [2], 2: [], 3: []}
    assert levels(dag) == {0: 2, 1: 1, 2: 0, 3: 0}


def test_transitive_closure_is_reflexive() -> None:
    dag = {0: [1], 1: [2], 2: []}
    reach = transitive_closure(dag)
    assert reach[0] == 0b111
    assert reach[2] == 0b100


def test_shortest_path_names_the_edges_to_remove() -> None:
    graph = {"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": []}
    assert shortest_path(graph, "a", "d") in (["a", "b", "d"], ["a", "c", "d"])
    assert shortest_path(graph, "d", "a") is None
    assert shortest_path(graph, "a", "a") == ["a"]


def test_deep_chains_do_not_hit_the_recursion_limit() -> None:
    """Real dependency chains exceed Python's 1000-frame default; the crash is opaque."""
    depth = 5000
    graph = {index: [index + 1] for index in range(depth)}
    graph[depth] = []
    assert len(strongly_connected_components(graph)) == depth + 1
    assert cycles(graph) == []


# ---- against networkx --------------------------------------------------------------------


@requires_networkx
@pytest.mark.parametrize("seed", [1, 2, 3, 7, 11])
def test_scc_matches_networkx_on_random_graphs(seed: int) -> None:
    networkx = _networkx()
    graph = random_graph(60, 150, seed)
    theirs = {frozenset(c) for c in networkx.strongly_connected_components(_to_nx(graph, networkx))}
    ours = {frozenset(c) for c in strongly_connected_components(graph)}
    assert ours == theirs


@requires_networkx
@pytest.mark.parametrize("seed", [4, 5, 6])
def test_transitive_closure_matches_networkx(seed: int) -> None:
    networkx = _networkx()
    graph = random_graph(40, 90, seed)
    _, dag, components = condensation(graph)
    reach = transitive_closure(dag)

    nx_dag = _to_nx(dag, networkx)
    for node in dag:
        ours = {index for index in range(len(components)) if reach[node] >> index & 1}
        theirs = set(networkx.descendants(nx_dag, node)) | {node}
        assert ours == theirs


@pytest.mark.parametrize("seed", [8, 9])
def test_topological_order_is_valid(seed: int) -> None:
    graph = random_graph(50, 80, seed)
    _, dag, _ = condensation(graph)
    order = topological_order(dag)
    position = {node: index for index, node in enumerate(order)}
    assert len(order) == len(dag)
    for node, successors in dag.items():
        for successor in successors:
            assert position[node] < position[successor]


@requires_networkx
@pytest.mark.parametrize("seed", [12, 13])
def test_shortest_path_length_matches_networkx(seed: int) -> None:
    networkx = _networkx()
    graph = random_graph(40, 100, seed)
    nx_graph = _to_nx(graph, networkx)
    for source in list(graph)[:10]:
        for target in list(graph)[:10]:
            ours = shortest_path(graph, source, target)
            try:
                theirs = networkx.shortest_path(nx_graph, source, target)
            except networkx.NetworkXNoPath:
                assert ours is None
                continue
            assert ours is not None
            assert len(ours) == len(theirs)


def test_modularity_is_higher_for_a_partition_that_matches_the_structure() -> None:
    """Two tight clusters with one bridge: the true partition must score highest."""
    graph = {
        "a1": ["a2", "a3"],
        "a2": ["a1", "a3"],
        "a3": ["a1", "a2"],
        "b1": ["b2", "b3"],
        "b2": ["b1", "b3"],
        "b3": ["b1", "b2", "a1"],
    }
    true_partition = {n: n[0] for n in graph}
    wrong_partition = {n: n[-1] for n in graph}
    assert modularity(graph, true_partition) > modularity(graph, wrong_partition)


def _to_nx(graph, networkx):
    nx_graph = networkx.DiGraph()
    nx_graph.add_nodes_from(graph)
    for node, successors in graph.items():
        for successor in successors:
            nx_graph.add_edge(node, successor)
    return nx_graph
