"""Dependency cycles and the smallest edit that would break each one.

Separate from `graph.architecture` because it answers a different question. Everything there
*measures* a component graph -- Martin's metrics, Lakos levels, propagation cost -- and a
measurement has no opinion about what to change. A feedback arc set is a proposed repair, it
is the one architectural finding a tool can turn into a concrete edit, and it is the only
part of the analysis that has an optional solver behind it. Keeping it here means the metrics
module never mentions clingo and `graph.algos` stays the zero-dependency layer it was written
to be (ADR-0001).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from oxn.graph.algos import cycles, minimum_feedback_arcs

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable, Mapping, Sequence

#: A ring, and the fewest edges whose removal breaks it -- ``None`` where no method decided.
Cut = tuple[tuple[str, str], ...] | None


def rings_and_cuts(graph: Mapping[str, Iterable[str]]) -> tuple[list[list[str]], list[Cut]]:
    """Every dependency cycle, and for each the fewest edges that would break it.

    The two are returned together and positionally aligned: a ring and its cut are one answer
    to one question, and keeping them in step is what lets a caller zip them strictly rather
    than hoping they correspond.
    """
    found = [sorted(cycle) for cycle in cycles(graph)]
    return found, [_cut(_ring_subgraph(graph, ring)) for ring in found]


def _cut(ring: Mapping[str, Iterable[str]]) -> Cut:
    """The exact cut, escalating to a solver only for rings too large to decide here.

    The order is the point. The zero-dependency dynamic program answers 19 of the 21 rings
    measured across seven projects (`scripts/measure_cycles.py`), so the `oxn[asp]` extra is
    what a project installs to improve two answers rather than to get any -- and a tool that
    reached for a solver first would make clingo a dependency in practice for a result it is
    not needed to produce.

    Reducing a ring before solving it -- contracting components whose in- or out-degree
    inside the ring is one -- could plausibly bring both of `typescript-nest`'s large rings
    under the exact limit and remove the need for a solver at all. It is not built: the
    reductions have to be proved cut-preserving and they turn the problem weighted, which is
    real work to save an optional extra that answers those two rings in 0.4 seconds.
    """
    exact = minimum_feedback_arcs(ring)
    if exact is not None:
        return exact
    from oxn.graph.asp import feedback_arcs_via_asp

    return feedback_arcs_via_asp(ring)


def _ring_subgraph(
    graph: Mapping[str, Iterable[str]], members: Sequence[str]
) -> dict[str, list[str]]:
    """The cycle on its own, with every edge leaving it dropped.

    A feedback arc set is computed per ring rather than over the whole graph because a
    strongly connected component is exactly the unit that has to be broken: edges between
    components already run one way, and including them only enlarges a search whose answer
    they cannot change.
    """
    inside = set(members)
    return {
        node: [target for target in graph.get(node, ()) if target in inside] for node in members
    }
