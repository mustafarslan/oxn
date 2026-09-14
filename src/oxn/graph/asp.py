"""Answer-set programming for the cycles the exact dynamic program will not decide.

**Optional, report-path only, and never the first thing tried.** `algos.minimum_feedback_arcs`
decides a ring of up to `EXACT_CUT_LIMIT` components exactly with no dependency at all, and
that covers 19 of the 21 rings measured across seven projects
(`scripts/measure_cycles.py`). This module exists for the other two, both in
`typescript-nest` at 24 and 25 components, where the dynamic program's ``O(2^n * n)`` stops
being affordable and the honest alternative to a solver is no answer.

`clingo` ships as the `oxn[asp]` extra, exactly as `networkx` stays a test-only oracle
(ADR-0001): it is imported inside the function, it is reached only for rings above the exact
limit, and `oxn check` -- the hook path -- never calls `analyse` at all. A missing install is
not an error here; it returns `None`, the same answer the caller already handles.

**Both solvers were cross-checked against each other before this was wired in.** On the ten
`typescript-nest` rings the dynamic program can decide, clingo proves the identical minimum
every time (`tests/test_graph_rings.py`). Two independent methods agreeing on ten instances is
a stronger claim for the solver's two answers than either makes alone.

**The search strategy is not a default worth leaving alone**, and this was measured rather
than assumed. clasp's out-of-the-box branch-and-bound does not prove either of nest's rings
optimal in 20 seconds -- it reaches 25 where the answer is 19. Core-guided optimisation
proves both in under 0.4 s single-threaded. Threads were the first explanation and the wrong
one: a portfolio just happened to include a core-guided configuration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable, Mapping

#: Minimum feedback arc set as an ordering problem, which is the same problem: place every
#: component in a distinct position, and an edge that points backwards must be cut.
#:
#: Ordering rather than reachability deliberately. Encoding "the result has no cycle" needs
#: transitive closure, and proving *optimality* over that is where a solver stalls -- clingo
#: proves both of nest's rings in under a third of a second this way.
_ENCODING = """
1 { at(V,P) : pos(P) } 1 :- node(V).
:- pos(P), 2 { at(V,P) : node(V) }.
cut(V,W) :- edge(V,W), at(V,PV), at(W,PW), PW < PV.
#minimize { 1,V,W : cut(V,W) }.
#show cut/2.
"""


def feedback_arcs_via_asp(
    graph: Mapping[str, Iterable[str]], *, seconds: float = 30.0
) -> tuple[tuple[str, str], ...] | None:
    """The minimum feedback arc set, or ``None`` when clingo cannot answer.

    ``None`` covers three cases the caller treats alike -- clingo is not installed, the
    search ran out of ``seconds``, or optimality was not proved. **A cut that is merely the
    best found so far is not returned**, because the whole value of the number is that it is
    minimal: "remove these 19 imports" invites the reader to check, and "remove these 21,
    probably" does not survive the first person who finds 19.
    """
    try:
        import clingo
    except ImportError:
        return None

    nodes = sorted(graph)
    loops = tuple((node, node) for node in nodes if node in graph[node])
    best: list[list[tuple[str, str]]] = []
    try:
        control = clingo.Control(["--opt-mode=opt", "--opt-strategy=usc,oll"])
        control.add("base", [], _program(graph, nodes))
        control.ground([("base", [])])
        found = control.solve(on_model=lambda model: best.append(_edges(model, nodes)), async_=True)
        if not found.wait(seconds):
            found.cancel()
        if not found.get().exhausted:
            return None
    except RuntimeError:
        # A clingo whose options this build does not accept, which is the shape every version
        # skew takes here. `oxn arch` reports the ring without a cut rather than failing over
        # an optional extra.
        return None
    return loops + (tuple(sorted(best[-1])) if best else ())


def _program(graph: Mapping[str, Iterable[str]], nodes: list[str]) -> str:
    """The ring as facts, plus the encoding above.

    **A self-loop is left out and cut outright by the caller.** The encoding orders the
    components and cuts what points backwards, and no ordering puts a node before itself:
    `PW < PV` cannot hold when `V` and `W` are the same, so clingo would report a ring it had
    not broken. `algos.minimum_feedback_arcs` had the same blind spot for the same reason,
    and the two agreeing is what made it visible.
    """
    index = {node: position for position, node in enumerate(nodes)}
    facts = [f"pos(1..{len(nodes)})."]
    facts += [f"node({position})." for position in range(len(nodes))]
    facts += [
        f"edge({index[node]},{index[target]})."
        for node in nodes
        for target in graph[node]
        if target in index and target != node
    ]
    return "\n".join(facts) + _ENCODING


def _edges(model: object, nodes: list[str]) -> list[tuple[str, str]]:
    """The ``cut/2`` atoms of one model, back in the caller's own component names."""
    shown = model.symbols(shown=True)  # type: ignore[attr-defined]
    return [
        (nodes[atom.arguments[0].number], nodes[atom.arguments[1].number])
        for atom in shown
        if atom.name == "cut"
    ]
