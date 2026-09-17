"""Graph algorithms, against hand-computed answers and networkx.

networkx is a *test* oracle only. It costs a measured 146 ms to import, most of the hook's
budget, which is why these ~250 lines exist at all (ADR-0001). `tests/test_import_guard.py`
fails the build if it ever reaches runtime.
"""

from __future__ import annotations

import itertools
import random

import pytest

from oxn.graph.algos import (
    condensation,
    cycles,
    levels,
    minimum_feedback_arcs,
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


# ---- discovered modules against declared ones (P12) ------------------------------------------


def test_louvain_is_pinned_so_two_runs_agree() -> None:
    """An erosion signal that moves between runs is not one.

    Louvain is randomised; `scripts/discovered_modules.py` pins the seed and the resolution for
    that reason, and this is the assertion that the pin is load-bearing rather than decorative.
    """
    pytest.importorskip("networkx")
    from scripts.discovered_modules import discovered_partition

    # Two triangles joined by a single edge: the textbook case where the communities are not
    # in dispute, so an unstable answer could only come from the search and not the graph.
    graph = {
        "a.py": {"b.py", "c.py"},
        "b.py": {"a.py", "c.py"},
        "c.py": {"a.py", "b.py", "z.py"},
        "x.py": {"y.py", "z.py"},
        "y.py": {"x.py", "z.py"},
        "z.py": {"x.py", "y.py", "c.py"},
    }

    first = discovered_partition(graph)
    assert first == discovered_partition(graph) == discovered_partition(graph)
    assert first["a.py"] == first["b.py"] == first["c.py"]
    assert first["x.py"] == first["y.py"] == first["z.py"]
    assert first["a.py"] != first["x.py"], "two triangles joined by one edge are two communities"


def test_a_declared_module_scattered_across_communities_is_the_signal() -> None:
    """The reported disagreement is "how many discovered communities does this declared
    component's code fall into" -- one means the directory and the dependency structure agree
    about it, several means files sharing a directory do not share a neighbourhood."""
    from scripts.discovered_modules import disagreements

    declared = {"a.py": "pkg", "b.py": "pkg", "c.py": "other", "d.py": "other"}
    discovered = {"a.py": "c0", "b.py": "c1", "c.py": "c2", "d.py": "c2"}

    assert disagreements(declared, discovered) == [("pkg", 2), ("other", 1)]


def test_the_gap_is_reported_and_not_interpreted() -> None:
    """**The measured result is that the raw gap does not discriminate.**

    Four projects: OXN +0.2003, go-kit +0.2933, nest +0.2767, httpx +0.1062. Every one is
    large, OXN's is the smallest of the three bigger projects, and Q(declared) spans only
    +0.0875 to +0.1289 across an order of magnitude of size and four languages. Louvain
    maximises Q by construction and a directory tree is not trying to, so the gap is dominated
    by that rather than by how any of them is organised.

    This test exists to keep the script honest about it: the docstring must not start claiming
    the gap means erosion without the null model or the over-time comparison that would earn it.
    """
    from scripts import discovered_modules

    text = discovered_modules.__doc__ or ""
    assert "does not discriminate" in text
    assert "not" in text and "gap = erosion" in text


# ---- the minimum feedback arc set ------------------------------------------------------


def _acyclic(graph: dict[int, list[int]], removed: set[tuple[int, int]]) -> bool:
    left = {
        node: [t for t in targets if (node, t) not in removed] for node, targets in graph.items()
    }
    return not cycles(left)


def _brute_force_minimum(graph: dict[int, list[int]]) -> int:
    """The smallest number of edges whose removal makes ``graph`` acyclic, by exhaustion.

    Exponential in the edge count and correct by construction, which is exactly what an
    oracle for an optimisation routine has to be: a greedy or an approximate implementation
    passes "the graph came out acyclic" and fails this.
    """
    edges = [(node, target) for node, targets in graph.items() for target in targets]
    for size in range(len(edges) + 1):
        for candidate in itertools.combinations(edges, size):
            if _acyclic(graph, set(candidate)):
                return size
    raise AssertionError("removing every edge did not make the graph acyclic")


@pytest.mark.parametrize("seed", range(12))
def test_feedback_arcs_are_minimum_not_merely_sufficient(seed: int) -> None:
    """Against exhaustive search. This is the test a greedy implementation fails."""
    graph = random_graph(6, 12, seed)
    cut = minimum_feedback_arcs(graph)
    assert cut is not None
    assert _acyclic(graph, set(cut))
    assert len(cut) == _brute_force_minimum(graph)


def test_one_cut_breaks_two_rings_that_share_an_edge() -> None:
    """Hand-computed: both cycles run through ``a -> b``, so one edge suffices.

    An implementation that breaks cycles one at a time cuts two edges here and is still
    "correct" by the weaker standard of leaving an acyclic graph behind.
    """
    graph = {"a": ["b"], "b": ["c", "d"], "c": ["a"], "d": ["a"]}
    assert minimum_feedback_arcs(graph) == (("a", "b"),)


def test_a_self_loop_is_always_cut() -> None:
    """No ordering places a node after itself, so the dynamic program cannot see this one.

    Found by the exhaustive oracle above, not by inspection: the first implementation
    returned a cut that left the self-loop in place and every other assertion still held.
    """
    assert minimum_feedback_arcs({"a": ["a", "b"], "b": []}) == (("a", "a"),)


def test_an_acyclic_graph_needs_no_cut() -> None:
    assert minimum_feedback_arcs({"a": ["b"], "b": ["c"], "c": []}) == ()


def test_a_ring_above_the_limit_is_declined_rather_than_guessed() -> None:
    """``None`` and ``()`` are different answers: no cut needed versus no cut computed.

    Sized like the two rings this actually declines, which are `typescript-nest`'s at 24 and
    25 components. The limit is a parameter so a report path can spend longer deliberately;
    that it is honoured downwards is what the second assertion pins.
    """
    ring = {index: [(index + 1) % 25] for index in range(25)}
    assert minimum_feedback_arcs(ring) is None
    assert minimum_feedback_arcs({0: [1], 1: [0]}, limit=1) is None


def test_the_chosen_cut_is_reproducible() -> None:
    """Minimal sets are not unique -- two components each importing the other have two.

    Which one is returned must not depend on dictionary order, or a repair suggestion would
    change between runs on an unchanged repository.
    """
    forwards = {"src/oxn": ["src/oxn/vcs"], "src/oxn/vcs": ["src/oxn"]}
    backwards = {"src/oxn/vcs": ["src/oxn"], "src/oxn": ["src/oxn/vcs"]}
    assert minimum_feedback_arcs(forwards) == minimum_feedback_arcs(backwards)
    assert len(minimum_feedback_arcs(forwards)) == 1
