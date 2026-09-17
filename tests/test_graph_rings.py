"""Dependency cycles and the smallest edit that breaks each one.

The interesting test here is the last one. `oxn[asp]` exists to answer rings the exact
dynamic program declines, which by definition are rings nothing else can check -- so the
claim that clingo's answers are right rests on the two methods agreeing everywhere they
overlap. That is asserted, on random graphs, rather than asserted once by hand.
"""

from __future__ import annotations

import pytest

from oxn.graph.algos import minimum_feedback_arcs
from oxn.graph.rings import rings_and_cuts
from test_graph_algos import random_graph

#: clingo is an optional extra, never a runtime dependency (ADR-0001, `oxn[asp]`).
requires_clingo = pytest.mark.oracle


def test_a_ring_comes_back_with_the_edges_that_break_it() -> None:
    graph = {"a": ["b"], "b": ["a"], "c": ["a"]}
    found, cuts = rings_and_cuts(graph)
    assert found == [["a", "b"]]
    assert cuts == [(("b", "a"),)]


def test_an_acyclic_graph_has_neither() -> None:
    assert rings_and_cuts({"a": ["b"], "b": []}) == ([], [])


def test_each_ring_is_cut_on_its_own() -> None:
    """Two disjoint rings are two problems, and an edge between them is neither's business.

    Cutting the pair jointly would give the same answer more slowly; the reason it matters is
    that the search is exponential in the ring's size, so joining them is how a tree of small
    rings becomes one instance nothing can decide.
    """
    graph = {"a": ["b"], "b": ["a"], "c": ["d"], "d": ["c"], "b/x": ["c"]}
    found, cuts = rings_and_cuts(graph)
    assert found == [["a", "b"], ["c", "d"]]
    assert [len(cut or ()) for cut in cuts] == [1, 1]


def test_the_cut_is_reported_against_the_ring_not_the_whole_graph() -> None:
    """An edge leaving the ring can never lie on a cycle, so it must never be suggested."""
    graph = {"a": ["b", "out"], "b": ["a"], "out": []}
    _, cuts = rings_and_cuts(graph)
    assert cuts == [(("b", "a"),)]


@requires_clingo
@pytest.mark.parametrize("seed", range(10))
def test_the_solver_and_the_exact_program_agree(seed: int) -> None:
    """Two independent methods, one answer. This is what licenses `oxn[asp]`'s two answers.

    Measured the same way on `typescript-nest`'s real rings: on all ten the dynamic program
    can decide, clingo proves the identical minimum.
    """
    pytest.importorskip("clingo")
    from oxn.graph.asp import feedback_arcs_via_asp

    graph = random_graph(7, 14, seed)
    exact = minimum_feedback_arcs(graph)
    solved = feedback_arcs_via_asp(graph)
    assert exact is not None
    assert solved is not None
    assert len(solved) == len(exact)


@requires_clingo
def test_the_solver_answers_the_same_way_every_time() -> None:
    """Two components importing each other have two equally minimal cuts, and one must win.

    Nothing in an optimising solver promises *which* optimal model comes back. A repair
    suggestion that alternates between runs on an unchanged repository reads as the tool
    changing its mind, so the options in `asp.py` are pinned to a single-threaded,
    fixed-strategy search and this is what holds them there.
    """
    pytest.importorskip("clingo")
    from oxn.graph.asp import feedback_arcs_via_asp

    ring = {"src/oxn": ["src/oxn/vcs"], "src/oxn/vcs": ["src/oxn"]}
    answers = {feedback_arcs_via_asp(ring) for _ in range(3)}
    assert len(answers) == 1
    assert len(answers.pop()) == 1


@requires_clingo
def test_the_solver_returns_nothing_for_an_acyclic_graph() -> None:
    pytest.importorskip("clingo")
    from oxn.graph.asp import feedback_arcs_via_asp

    assert feedback_arcs_via_asp({"a": ["b"], "b": []}) == ()
