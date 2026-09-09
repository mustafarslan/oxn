r"""LCOM: Lack of Cohesion of Methods, in the several incompatible senses of the term.

Six published definitions share the name and disagree with each other, which is why LCOM is
the most misreported metric in the industry. OXN computes them all and **says which is
which**:

* **LCOM1** -- Chidamber & Kemerer, *OOPSLA 1991*: method pairs sharing no field.
* **LCOM2** -- CK, *IEEE TSE* 20(6), 1994: ``max(0, |P| - |Q|)``.
* **LCOM3** -- Li & Henry, *JSS* 23(2), 1993: connected components of the field-sharing graph.
* **LCOM4** -- Hitz & Montazeri, 1995: LCOM3 *plus* intra-class call edges. The actionable
  one: "this class splits cleanly into 3 pieces".

Both component variants **exclude constructors**, which is a departure from the definitions
as published and was carried here for a while as "a known property rather than a defect": a
constructor touching every field shares one with every method, so it joins every cluster
through itself. A control pair settled it -- `tests/fixtures/cohesion_shapes` holds a wide
repository and a five-collaborator God Class at the same method count, and with constructors
counted the God Class reported *one* component, the same answer as the repository, which is
the false negative landing precisely on the shape the metric exists to find. Excluded, it
reports five, and they are its five collaborators. LCOM1, LCOM2 and LCOM\* still count
constructors: they are ratios over pairs rather than a partition, the argument above does not
transfer, and no control here measures them.
* **LCOM\\*** (LCOM5) -- Henderson-Sellers, 1996: normalised to roughly ``[0, 1]``, so it is
  the one to compare across classes.

OXN headlines LCOM\\* and LCOM4 and reports the rest for compatibility.

Everything here is a pure function of the class member model, so its exactness is that
model's: EXACT for explicitly-qualified access, APPROX once an inherited or dynamic access
has been counted.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable

    from oxn.resolve.members import ClassModel


@dataclass(frozen=True, slots=True)
class Cohesion:
    """Every LCOM variant for one class, plus what it took to compute them."""

    class_name: str
    method_count: int
    field_count: int
    lcom1: int
    lcom2: int
    lcom3: int
    lcom4: int
    lcom_star: float | None
    connectivity: float | None
    exactness: str

    def as_dict(self) -> dict[str, object]:
        return {
            "class": self.class_name,
            "methods": self.method_count,
            "fields": self.field_count,
            "lcom1": self.lcom1,
            "lcom2": self.lcom2,
            "lcom3": self.lcom3,
            "lcom4": self.lcom4,
            "lcom_star": None if self.lcom_star is None else round(self.lcom_star, 4),
            "connectivity": None if self.connectivity is None else round(self.connectivity, 4),
            "exactness": self.exactness,
        }


def cohesion(model: ClassModel) -> Cohesion:
    """Compute every LCOM variant for one class."""
    methods = sorted(model.methods)
    method_count = len(methods)
    fields = sorted(model.fields)

    sharing = 0
    not_sharing = 0
    for first, second in model.method_pairs():
        shared = model.methods[first].touches & model.methods[second].touches
        if shared:
            sharing += 1
        else:
            not_sharing += 1

    return Cohesion(
        class_name=model.name,
        method_count=method_count,
        field_count=len(fields),
        lcom1=not_sharing,
        lcom2=max(0, not_sharing - sharing),
        lcom3=_components(model, include_calls=False),
        lcom4=_components(model, include_calls=True),
        lcom_star=_lcom_star(model),
        connectivity=_connectivity(model),
        exactness="APPROX" if model.is_approximate else "EXACT",
    )


class _DisjointSet:
    """Union-find over method names, with path halving.

    A class rather than two closures inside `_components`: a nested function raises the
    nesting level of everything around it, so the whole component scan was scored two
    levels deep for a reason that was about scoping rather than about the algorithm.
    """

    __slots__ = ("parent",)

    def __init__(self, names: Iterable[str]) -> None:
        self.parent = {name: name for name in names}

    def __contains__(self, name: str) -> bool:
        return name in self.parent

    def find(self, name: str) -> str:
        while self.parent[name] != name:
            self.parent[name] = self.parent[self.parent[name]]
            name = self.parent[name]
        return name

    def union(self, first: str, second: str) -> None:
        left, right = self.find(first), self.find(second)
        if left != right:
            self.parent[right] = left

    def count(self) -> int:
        return len({self.find(name) for name in self.parent})


#: Method names that initialise a class rather than use it. Java's constructor is the
#: class's own name and is matched separately; Go has none. Rust's `new` is deliberately
#: absent -- it is a naming convention for an associated function, not a constructor the
#: language knows about, so excluding it would drop a real method from the scan.
_CONSTRUCTOR_NAMES = frozenset({"__init__", "__new__", "constructor"})


def _participants(model: ClassModel) -> list[str]:
    """The methods the component scan runs over: everything but the constructors.

    A class whose *only* method is its constructor keeps it, because the alternative is
    reporting zero components for a class that plainly has one.
    """
    constructors = {
        name for name in model.methods if name in _CONSTRUCTOR_NAMES or name == model.name
    }
    return sorted(set(model.methods) - constructors) or sorted(model.methods)


def _components(model: ClassModel, *, include_calls: bool) -> int:
    """Connected components of the method graph.

    Two methods are joined when they touch a common field, and -- for LCOM4 -- when one
    calls the other. A class that is really two classes shows up as two components.

    **Constructors are excluded**, and `tests/test_wide_repository.py` is why. A constructor
    that assigns every field shares a field with every method, so it joins every component
    through itself and a five-collaborator God Class reads as one cohesive unit -- the false
    negative landing exactly on the shape these variants exist to find. Excluding it is the
    difference between "this class is fine" and five components that name its five seams.

    Both variants share the exclusion rather than LCOM4 taking it alone: LCOM4 is LCOM3 plus
    edges, edges can only merge components, so `lcom4 <= lcom3` holds by definition and a
    filter on one side alone would break it.
    """
    if not model.methods:
        return 0

    names = _participants(model)
    groups = _DisjointSet(names)
    _join_shared_fields(model, names, groups)
    if include_calls:
        _join_callers(model, names, groups)
    return groups.count()


def _join_shared_fields(model: ClassModel, names: list[str], groups: _DisjointSet) -> None:
    """Two methods that touch a common field belong to the same component (LCOM1-5)."""
    for first, second in combinations(names, 2):
        if model.methods[first].touches & model.methods[second].touches:
            groups.union(first, second)


def _join_callers(model: ClassModel, names: list[str], groups: _DisjointSet) -> None:
    """LCOM4 additionally joins a method to the siblings it calls.

    Only siblings: a call to something outside the class says nothing about *this* class's
    cohesion, which is why the membership test is against the group rather than the name.
    A call *to* a constructor finds nothing in the group, which is the exclusion holding.
    """
    for name in names:
        for callee in model.methods[name].calls:
            if callee in groups:
                groups.union(name, callee)


def _lcom_star(model: ClassModel) -> float | None:
    """Henderson-Sellers LCOM*.

    ``((1/a) * sum over fields of the methods touching it - m) / (1 - m)``. Undefined for a
    class with no fields or a single method -- reporting 0 or 1 there would invent cohesion
    information the class does not carry, the same reasoning as abstractness with no types.

    Values above 1 are legitimate and mean fields are declared but unused by any method.
    """
    method_count = len(model.methods)
    fields = sorted(model.fields)
    if method_count <= 1 or not fields:
        return None

    total = 0
    for name in fields:
        total += sum(1 for access in model.methods.values() if name in access.touches)

    mean_access = total / len(fields)
    value = (mean_access - method_count) / (1 - method_count)
    return value + 0.0  # normalise -0.0, which reads as a bug in a report


def _connectivity(model: ClassModel) -> float | None:
    """Hitz-Montazeri connectivity, which discriminates classes where LCOM4 = 1.

    ``C = 2(|E| - (n-1)) / ((n-1)(n-2))``: how much more connected than a bare spanning
    tree. Undefined below three methods, where the formula divides by zero.
    """
    methods = sorted(model.methods)
    count = len(methods)
    if count < 3:
        return None

    edges = 0
    for first, second in model.method_pairs():
        shared = bool(model.methods[first].touches & model.methods[second].touches)
        calls = second in model.methods[first].calls or first in model.methods[second].calls
        if shared or calls:
            edges += 1

    return 2 * (edges - (count - 1)) / ((count - 1) * (count - 2))
