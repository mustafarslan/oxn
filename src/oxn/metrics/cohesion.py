r"""LCOM: Lack of Cohesion of Methods, in the several incompatible senses of the term.

Six published definitions share the name and disagree with each other, which is why LCOM is
the most misreported metric in the industry. OXN computes them all and **says which is
which**:

* **LCOM1** -- Chidamber & Kemerer, *OOPSLA 1991*: method pairs sharing no field.
* **LCOM2** -- CK, *IEEE TSE* 20(6), 1994: ``max(0, |P| - |Q|)``.
* **LCOM3** -- Li & Henry, *JSS* 23(2), 1993: connected components of the field-sharing graph.
  Note a known property rather than a defect: a constructor touching every field joins every
  cluster, so a class that is really two classes still reports one component if its
  ``__init__`` initialises both halves. LCOM4's call edges have the same effect. This is
  what the definitions say; it is the reason LCOM\* is the headline number.
* **LCOM4** -- Hitz & Montazeri, 1995: LCOM3 *plus* intra-class call edges. The actionable
  one: "this class splits cleanly into 3 pieces".
* **LCOM\\*** (LCOM5) -- Henderson-Sellers, 1996: normalised to roughly ``[0, 1]``, so it is
  the one to compare across classes.

OXN headlines LCOM\\* and LCOM4 and reports the rest for compatibility.

Everything here is a pure function of the class member model, so its exactness is that
model's: EXACT for explicitly-qualified access, APPROX once an inherited or dynamic access
has been counted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
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


def _components(model: ClassModel, *, include_calls: bool) -> int:
    """Connected components of the method graph.

    Two methods are joined when they touch a common field, and -- for LCOM4 -- when one
    calls the other. A class that is really two classes shows up as two components.
    """
    methods = sorted(model.methods)
    if not methods:
        return 0

    parent = {name: name for name in methods}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(first: str, second: str) -> None:
        left, right = find(first), find(second)
        if left != right:
            parent[right] = left

    for first, second in model.method_pairs():
        if model.methods[first].touches & model.methods[second].touches:
            union(first, second)

    if include_calls:
        for name, access in model.methods.items():
            for callee in access.calls:
                if callee in parent:
                    union(name, callee)

    return len({find(name) for name in methods})


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
