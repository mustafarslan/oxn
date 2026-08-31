"""Evaluate rules against facts, and refuse to evaluate ones that are not well formed.

Bottom-up and set-at-a-time, which is the shape a semi-naive Datalog engine has: seed with
the extensional database, join each body left to right, collect what satisfies the whole
body. Joins are hash joins on the columns already bound when an atom is reached.

**This evaluator is written to be replaced.** Swapping in a real Datalog engine should mean
replacing `evaluate` and nothing else, because the rule syntax, the relation schema and the
well-formedness rules are specified in ADR-0005 rather than implied here.

Two checks run before evaluation and raise rather than producing plausible output:

* **range restriction** -- every variable in a comparison, a negated atom or the head is
  bound by a positive atom earlier in the body;
* **stratification** -- no rule negates a relation that rules in its own stratum derive.

The first rule set exercises neither recursion nor non-trivial stratification (ADR-0005's
appendix: all four contract kinds are edge joins, nothing needs reachability). The checks
exist anyway, and are tested directly, so the day a recursive rule arrives they have been
run rather than assumed.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from oxn.rules.model import Atom, Compare, Rule, Var

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator

    from oxn.rules.model import Condition, Facts, Term

#: The comparisons a body may use. Deliberately small: ADR-0005 fixes the built-in surface
#: at twelve relations and arithmetic, and every operator here is one a Datalog engine has.
OPERATORS = {
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


class RuleError(ValueError):
    """A rule that cannot be evaluated soundly. Raised at load time, never at query time."""


@dataclass(frozen=True, slots=True)
class RuleFinding:
    """One satisfied rule, in the shape `oxn.check.Finding` needs.

    A separate type because `rules` sits below `check` in the layer contract: findings flow
    *up* to the surface that reports them, and the rule engine does not import the reporter.
    """

    rule: str
    path: str
    entity: str
    line: int
    value: float
    ceiling: float
    blocking: bool
    explanation: tuple[str, ...] = ()
    detail: str = ""


def evaluate(rules: list[Rule], facts: Facts) -> list[RuleFinding]:
    """Every finding the rules produce against these facts, checked first and then run."""
    for rule in rules:
        check_well_formed(rule)
    check_stratified(rules)
    return [_finding(rule, binding) for rule in rules for binding in _solve(rule.body, facts)]


def check_well_formed(rule: Rule) -> None:
    """Range restriction: nothing may be used before a positive atom binds it."""
    bound: set[str] = set()
    for condition in rule.body:
        _require_bound(rule, condition, bound)
        if isinstance(condition, Atom) and not condition.negated:
            bound |= condition.variables
    missing = rule.head.variables - bound
    if missing:
        raise RuleError(
            f"rule {rule.name!r} builds a finding from unbound variable(s) "
            f"{', '.join(sorted(missing))}; bind them in a positive atom first"
        )


def _require_bound(rule: Rule, condition: Condition, bound: set[str]) -> None:
    if isinstance(condition, Atom) and not condition.negated:
        return
    missing = condition.variables - bound
    if missing:
        kind = "negated atom" if isinstance(condition, Atom) else "comparison"
        raise RuleError(
            f"rule {rule.name!r} uses unbound variable(s) {', '.join(sorted(missing))} "
            f"in the {kind} `{condition}`; a positive atom must bind them first"
        )


def check_stratified(rules: list[Rule]) -> None:
    """No rule may negate a relation that its own stratum derives.

    With only extensional relations negated -- which is the whole of the current rule set --
    this is satisfied trivially. It is checked rather than assumed because a hand-rolled
    evaluator will happily run an unstratified rule and return something plausible, and
    that is precisely how a surface stops being Datalog-compatible without anyone noticing.
    """
    derived = {rule.name for rule in rules}
    for rule in rules:
        conflict = rule.negated_relations & derived
        if conflict:
            raise RuleError(
                f"rule {rule.name!r} negates derived relation(s) "
                f"{', '.join(sorted(conflict))}; that is not stratified"
            )


def _solve(body: tuple[Condition, ...], facts: Facts) -> Iterator[dict[str, Any]]:
    """Every binding satisfying the whole conjunction, joined left to right."""
    bindings: Iterator[dict[str, Any]] = iter([{}])
    for condition in body:
        bindings = _apply(condition, bindings, facts)
    return bindings


def _apply(
    condition: Condition, bindings: Iterator[dict[str, Any]], facts: Facts
) -> Iterator[dict[str, Any]]:
    if isinstance(condition, Compare):
        return (b for b in bindings if _compare(condition, b))
    if condition.negated:
        return (b for b in bindings if not _any_match(condition, b, facts))
    return _join(condition, bindings, facts)


def _join(atom: Atom, bindings: Iterator[dict[str, Any]], facts: Facts) -> Iterator[dict[str, Any]]:
    """Extend each binding with every tuple of `atom` that agrees with it."""
    tuples = facts.get(atom.relation)
    for binding in bindings:
        for row in tuples:
            extended = _unify(atom.terms, row, binding)
            if extended is not None:
                yield extended


def _any_match(atom: Atom, binding: dict[str, Any], facts: Facts) -> bool:
    return any(_unify(atom.terms, row, binding) is not None for row in facts.get(atom.relation))


def _unify(
    terms: tuple[Term, ...], row: tuple[Any, ...], binding: dict[str, Any]
) -> dict[str, Any] | None:
    """Match one tuple against a term list, or return None when they disagree.

    A repeated variable in one atom -- `edge(X, X)` -- must match the same value in both
    columns, which falls out of writing into the same key and comparing before writing.
    """
    if len(terms) != len(row):
        return None
    extended = dict(binding)
    for term, value in zip(terms, row, strict=True):
        if isinstance(term, Var):
            if extended.setdefault(term.name, value) != value:
                return None
        elif term != value:
            return None
    return extended


def _compare(condition: Compare, binding: dict[str, Any]) -> bool:
    left = _resolve(condition.left, binding)
    right = _resolve(condition.right, binding)
    return bool(OPERATORS[condition.op](left, right))


def _resolve(term: Term, binding: dict[str, Any]) -> Any:
    return binding[term.name] if isinstance(term, Var) else term


def _finding(rule: Rule, binding: dict[str, Any]) -> RuleFinding:
    head = rule.head
    return RuleFinding(
        rule=rule.name,
        path=str(_resolve(head.path, binding)),
        entity=str(_resolve(head.entity, binding)),
        line=int(_resolve(head.line, binding)),
        value=float(_resolve(head.value, binding)),
        ceiling=float(_resolve(head.ceiling, binding)),
        blocking=bool(_resolve(head.blocking, binding)),
        explanation=tuple(_resolve(head.explanation, binding)),
        detail=head.detail,
    )
