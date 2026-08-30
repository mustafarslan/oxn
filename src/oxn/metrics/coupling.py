"""The Chidamber & Kemerer suite (CK, *IEEE TSE* 20(6), 1994).

Each metric is stamped with the resolution it required, because they are not equally
knowable from syntax:

===== ============================== ===================================================
WMC   sum of method complexities     EXACT -- syntax alone
DIT   depth of inheritance tree      EXACT in-repo at L1; external bases counted as +1
NOC   number of children             EXACT in-repo; out-of-repo subclasses are unknowable
CBO   coupling between objects       EXACT over *confident* targets; APPROX otherwise
RFC   response for a class           EXACT-static over confident calls; a syntactic
                                     fallback counts distinct callee names instead
===== ============================== ===================================================

The confident/uncertain split is not a hedge: L1 reports 99.8% precision on the answers it
is certain of and 70.3% overall (docs/divergences.md), so a metric built only on certain
answers is trustworthy while one built on all of them is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping

    from oxn.resolve.members import ClassModel
    from oxn.resolve.symbols import ProjectSymbols


@dataclass(frozen=True, slots=True)
class CkMetrics:
    """The CK suite for one class."""

    class_name: str
    wmc: int
    nom: int
    dit: int
    noc: int
    cbo: int
    rfc: int
    rfc_syntactic: int
    dit_external_unresolved: bool
    exactness: str

    def as_dict(self) -> dict[str, object]:
        return {
            "class": self.class_name,
            "wmc": self.wmc,
            "nom": self.nom,
            "dit": self.dit,
            "noc": self.noc,
            "cbo": self.cbo,
            "rfc": self.rfc,
            "rfc_syntactic": self.rfc_syntactic,
            "dit_external_unresolved": self.dit_external_unresolved,
            "exactness": self.exactness,
        }


@dataclass
class ClassHierarchy:
    """In-repo inheritance, built from what every file declares."""

    bases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    children: dict[str, set[str]] = field(default_factory=dict)

    def add(self, name: str, bases: tuple[str, ...]) -> None:
        self.bases[name] = bases
        for base in bases:
            self.children.setdefault(base, set()).add(name)

    def depth(self, name: str) -> tuple[int, bool]:
        """``(depth, whether a base lies outside the tree)``.

        An external base -- ``BaseModel``, ``Exception`` -- contributes one level and a
        flag. Pretending its own depth is zero would silently understate every class that
        extends a library type.
        """
        seen: set[str] = set()
        external = False
        depth = 0
        frontier = [name]
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            bases = self.bases.get(current)
            if bases is None:
                if current != name:
                    external = True
                continue
            if bases:
                depth = max(depth, len(seen))
                frontier.extend(bases)
        return (depth + (1 if external else 0), external)

    def child_count(self, name: str) -> int:
        return len(self.children.get(name, ()))


def build_hierarchy(models_by_file: Mapping[str, Mapping[str, ClassModel]]) -> ClassHierarchy:
    hierarchy = ClassHierarchy()
    for models in models_by_file.values():
        for name, model in models.items():
            hierarchy.add(name, model.bases)
    return hierarchy


def ck_metrics(
    model: ClassModel,
    hierarchy: ClassHierarchy,
    *,
    path: str = "",
    symbols: ProjectSymbols | None = None,
    complexity: Mapping[str, int] | None = None,
) -> CkMetrics:
    """Compute the CK suite for one class."""
    weights = complexity or {}
    wmc = sum(weights.get(name, 1) for name in model.methods)
    depth, external = hierarchy.depth(model.name)

    external_calls: set[str] = set()
    confident: set[str] = set()
    uncertain = False

    for access in model.methods.values():
        for callee in access.calls:
            external_calls.add(callee)

    # Distinct callee names across the class, for the syntactic fallback.
    syntactic_targets = set(model.methods) | external_calls

    coupled: set[str] = set()
    for base in model.bases:
        if base in hierarchy.bases:
            coupled.add(base)
        else:
            uncertain = True

    if symbols is not None and path:
        for access in model.methods.values():
            for callee in access.calls:
                found = symbols.resolve_call(path, callee)
                if found is None:
                    uncertain = True
                elif found.is_certain:
                    confident.add(found.qualified_name)
                else:
                    uncertain = True

    rfc = len(model.methods) + len(confident)
    return CkMetrics(
        class_name=model.name,
        wmc=wmc,
        nom=len(model.methods),
        dit=depth,
        noc=hierarchy.child_count(model.name),
        cbo=len(coupled | confident),
        rfc=rfc,
        rfc_syntactic=len(syntactic_targets),
        dit_external_unresolved=external,
        exactness="APPROX" if (uncertain or model.is_approximate) else "EXACT",
    )
