"""The eleven checks OXN shipped as hand-written Python, expressed as rules.

This module is P7's exit criterion made concrete: *a rule set expressed purely in
`oxn.yaml` and ADRs reproduces the full hand-coded check suite*. Nothing here is a new
rule, and that is the point -- until these produce byte-identical findings to
`check._ceiling_findings` and `contracts.check_contracts`, the engine has not earned the
right to replace them.

The rules are built from config rather than hard-coded, because `oxn.yaml` decides which
ceilings exist and which contracts are declared. What is fixed is the *shape*; ADR-0005's
appendix wrote all eleven out on paper before any of this existed, which is how the
`blocking` design error was caught at doc cost rather than here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from oxn.config import GATED_METRICS
from oxn.rules.model import Atom, Compare, Head, Rule, Var

if TYPE_CHECKING:  # pragma: no cover
    from oxn.config import Config

#: Bound in every ceiling rule, so the bodies below read as one shape with two parameters.
E, PATH, KIND, NAME, LINE = Var("E"), Var("Path"), Var("Kind"), Var("Name"), Var("Line")
V, EX, B, BLK, TRAIL, LIMIT = Var("V"), Var("Ex"), Var("B"), Var("Blk"), Var("Trail"), Var("Limit")
SRC, DST, L1, L2, CONTRACT = Var("Src"), Var("Dst"), Var("L1"), Var("L2"), Var("C")


def ceiling_rules(settings: Config) -> list[Rule]:
    """One rule per declared ceiling: same body, two parameters.

    The metric key and the kind guard are what differ. `function_sloc` and `file_sloc` share
    the metric `sloc` and are two rules precisely because the guard differs -- 60 lines is a
    statement about a function, and applying it to a module flags every file there is.
    """
    return [_ceiling_rule(name, GATED_METRICS[name]) for name in settings.ceilings]


def _ceiling_rule(name: str, gate: object) -> Rule:
    metric_key = gate.metric  # type: ignore[attr-defined]
    guard = "callable" if "function" in gate.kinds else "file_kind"  # type: ignore[attr-defined]
    return Rule(
        name=name,
        body=(
            Atom("entity", (E, PATH, KIND, NAME, LINE)),
            Atom(guard, (KIND,)),
            Atom("metric", (E, metric_key, V, EX, B)),
            Atom("explanation", (E, metric_key, TRAIL)),
            # ADR-0002 as a join, not an `if`: an unsound measurement is still reported,
            # it simply cannot block. `Blk` is bound here and carried into the head.
            Atom("blocking", (EX, B, BLK)),
            Atom("ceiling", (name, PATH, LIMIT)),
            Compare(V, ">", LIMIT),
        ),
        head=Head(
            path=PATH,
            entity=NAME,
            line=LINE,
            value=V,
            ceiling=LIMIT,
            blocking=BLK,
            explanation=TRAIL,
        ),
    )


def contract_rules(settings: Config) -> list[Rule]:
    """One rule per declared contract. All four kinds are edge joins; none recurses."""
    builders = {
        "layered": _layered_rule,
        "forbidden": _forbidden_rule,
        "independence": _independence_rule,
        "deep_import": _deep_import_rule,
    }
    return [
        builders[contract.kind](contract)
        for contract in settings.contracts
        if contract.kind in builders
    ]


def _edge_body() -> tuple[Atom, ...]:
    """The join every contract kind starts from: one import edge, and both ends' layers.

    `imports` is already runtime-only: `build_dependency_graph` drops type-only edges before
    the graph exists, because a type-only import is erased before execution and is not
    coupling. That filter is upstream rather than a `runtime` atom here, so there is no way
    for a rule to forget it -- which matters, since this project once counted those edges
    and manufactured two layer violations from them.
    """
    return (
        Atom("imports", (SRC, DST)),
        Atom("layer", (SRC, L1)),
        Atom("layer", (DST, L2)),
    )


def _edge_head(contract_name: str, detail: str) -> Head:
    """Keyed by the offending *edge*, exactly as the hand-coded gate keys it.

    Keying on the layer pair instead makes the baseline an amnesty: a new violation between
    the same two layers lands on an existing entry with an identical value, so neither
    "new" nor "worse" can ever fire. This project shipped that once too.
    """
    return Head(path=SRC, entity=Var("Edge"), line=1, value=1.0, ceiling=0.0, detail=detail)


def _layered_rule(contract: object) -> Rule:
    name = contract.name  # type: ignore[attr-defined]
    return Rule(
        name=f"contract:{name}",
        body=(
            *_edge_body(),
            # Only layers the contract actually orders: an edge touching an unnamed layer
            # is outside this contract's jurisdiction, not a violation of it.
            Atom("in_contract", (name, L1)),
            Atom("in_contract", (name, L2)),
            Compare(L1, "!=", L2),
            Atom("allowed_edge", (name, L1, L2), negated=True),
            Atom("edge_label", (SRC, DST, Var("Edge"))),
        ),
        head=_edge_head(name, "depends on a layer above it"),
    )


def _forbidden_rule(contract: object) -> Rule:
    name = contract.name  # type: ignore[attr-defined]
    return Rule(
        name=f"contract:{name}",
        body=(
            *_edge_body(),
            Atom("forbidden_edge", (name, L1, L2)),
            Atom("edge_label", (SRC, DST, Var("Edge"))),
        ),
        head=_edge_head(name, "depends on a forbidden layer"),
    )


def _independence_rule(contract: object) -> Rule:
    name = contract.name  # type: ignore[attr-defined]
    return Rule(
        name=f"contract:{name}",
        body=(
            Atom("imports", (SRC, DST)),
            Atom("layer", (SRC, L1)),
            Atom("layer", (DST, L2)),
            Compare(L1, "!=", L2),
            Atom("independent", (name, L1)),
            Atom("independent", (name, L2)),
            Atom("edge_label", (SRC, DST, Var("Edge"))),
        ),
        head=_edge_head(name, "must remain independent"),
    )


def _deep_import_rule(contract: object) -> Rule:
    name = contract.name  # type: ignore[attr-defined]
    return Rule(
        name=f"contract:{name}",
        body=(
            Atom("imports", (SRC, DST)),
            Atom("layer", (DST, L2)),
            Atom("package", (name, L2)),
            Atom("layer", (SRC, L1)),
            Compare(L1, "!=", L2),
            Atom("entrypoint_match", (name, DST), negated=True),
            Atom("edge_label", (SRC, DST, Var("Edge"))),
        ),
        head=_edge_head(name, "entered outside its declared entry points"),
    )


def all_rules(settings: Config) -> list[Rule]:
    """Every rule the current configuration declares."""
    return [*ceiling_rules(settings), *contract_rules(settings)]
