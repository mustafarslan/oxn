"""What a rule *is*: relations, atoms, bodies and heads, with no evaluator in sight.

Separated from `engine.py` on purpose. This module is the surface ADR-0005 fixes -- the
thing a future Datalog engine has to honour -- and it is easier to keep a promise about a
module that cannot evaluate anything.

The vocabulary is Datalog's, deliberately and with its constraints intact:

* a **term** is a variable or a constant;
* an **atom** is a relation name plus one term per column, and may be negated;
* a **body** is a conjunction of atoms and comparisons -- never a disjunction, because two
  rules sharing a head are the disjunction, which is what keeps the surface translatable;
* every variable used in a comparison, a negated atom or the head must be **bound by a
  positive atom earlier in the body**. That is range restriction, and it is checked at load
  time rather than discovered as a confusing empty result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: A term is either a `Var` or a plain constant (str, int, float, bool).
Term = Any


@dataclass(frozen=True, slots=True)
class Var:
    """A logic variable. Two atoms sharing one are joined on that column."""

    name: str

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class Atom:
    """One relation lookup: `imports(S, T, K)`, or its negation.

    `negated` is the `absent` of the appendix in ADR-0005. A negated atom filters bindings
    already produced; it can never introduce one, which is why range restriction requires
    its variables to be bound earlier.
    """

    relation: str
    terms: tuple[Term, ...]
    negated: bool = False

    @property
    def variables(self) -> set[str]:
        return {term.name for term in self.terms if isinstance(term, Var)}

    def __str__(self) -> str:
        inside = ", ".join(str(term) for term in self.terms)
        return f"{'not ' if self.negated else ''}{self.relation}({inside})"


@dataclass(frozen=True, slots=True)
class Compare:
    """A comparison between two bound terms, or a bound term and a constant."""

    left: Term
    op: str
    right: Term

    @property
    def variables(self) -> set[str]:
        return {t.name for t in (self.left, self.right) if isinstance(t, Var)}

    def __str__(self) -> str:
        return f"{self.left} {self.op} {self.right}"


#: One element of a body: an atom to join, or a comparison to filter by.
Condition = Atom | Compare


@dataclass(frozen=True, slots=True)
class Head:
    """Which binding fills which `Finding` field.

    A message template alone cannot preserve finding identity, and identity is
    `rule|path|entity` -- the ratchet is built on it. So the mapping is part of the rule
    rather than something the evaluator infers.
    """

    path: Term
    entity: Term
    line: Term = 1
    value: Term = 1.0
    ceiling: Term = 0.0
    detail: str = ""

    @property
    def variables(self) -> set[str]:
        terms = (self.path, self.entity, self.line, self.value, self.ceiling)
        return {term.name for term in terms if isinstance(term, Var)}


@dataclass(frozen=True, slots=True)
class Rule:
    """One named rule: a conjunctive body and the finding its satisfaction produces."""

    name: str
    body: tuple[Condition, ...]
    head: Head
    #: False for a rule that reports without failing a build. A rule acquires blocking power
    #: by carrying the `blocking` atom, never by default -- see ADR-0005 section 4.
    blocking: bool = True

    @property
    def relations(self) -> set[str]:
        return {c.relation for c in self.body if isinstance(c, Atom)}

    @property
    def negated_relations(self) -> set[str]:
        return {c.relation for c in self.body if isinstance(c, Atom) and c.negated}


@dataclass
class Facts:
    """The extensional database: relation name -> the tuples it holds.

    Tuples are plain, so a relation is a set of them and nothing here knows what a metric
    or a layer means. That is the point: the evaluator joins columns, and the meaning lives
    in `facts.py` where the projection from the store happens.
    """

    relations: dict[str, set[tuple[Any, ...]]] = field(default_factory=dict)

    def add(self, relation: str, *tuples: tuple[Any, ...]) -> None:
        self.relations.setdefault(relation, set()).update(tuples)

    def get(self, relation: str) -> set[tuple[Any, ...]]:
        return self.relations.get(relation, set())

    def __contains__(self, relation: str) -> bool:
        return relation in self.relations
