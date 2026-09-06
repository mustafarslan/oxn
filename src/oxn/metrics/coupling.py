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


@dataclass(frozen=True, slots=True)
class Evidence:
    """What is known about a class's outgoing references, and how well.

    `path` and `symbols` travel together -- resolution is meaningless without both -- and
    `complexity` is the weighting table for WMC. Three optional arguments that are really
    one question: how much can be established about what this class reaches?
    """

    path: str = ""
    symbols: ProjectSymbols | None = None
    complexity: Mapping[str, int] | None = None

    @property
    def can_resolve(self) -> bool:
        return self.symbols is not None and bool(self.path)


def ck_metrics(
    model: ClassModel, hierarchy: ClassHierarchy, evidence: Evidence | None = None
) -> CkMetrics:
    """Compute the CK suite for one class."""
    evidence = evidence or Evidence()
    weights = evidence.complexity or {}
    wmc = sum(weights.get(name, 1) for name in model.methods)
    depth, external = hierarchy.depth(model.name)

    external_calls = {callee for access in model.methods.values() for callee in access.calls}
    coupled, bases_uncertain = _coupled_bases(model, hierarchy)
    confident, calls_uncertain = _confident_callees(model, evidence)

    return CkMetrics(
        class_name=model.name,
        wmc=wmc,
        nom=len(model.methods),
        dit=depth,
        noc=hierarchy.child_count(model.name),
        cbo=len(coupled | confident),
        rfc=len(model.methods) + len(confident),
        rfc_syntactic=len(set(model.methods) | external_calls),
        dit_external_unresolved=external,
        exactness=(
            "APPROX" if (bases_uncertain or calls_uncertain or model.is_approximate) else "EXACT"
        ),
    )


def _coupled_bases(model: ClassModel, hierarchy: ClassHierarchy) -> tuple[set[str], bool]:
    """Base classes CBO can count, and whether any could not be placed.

    A base OXN cannot find is not absent -- it is unknown, and the difference is the whole
    point of the exactness stamp: an unknown base means CBO is a lower bound, not a fact.
    """
    coupled = {base for base in model.bases if base in hierarchy.bases}
    return coupled, len(coupled) != len(model.bases)


def _confident_callees(model: ClassModel, evidence: Evidence) -> tuple[set[str], bool]:
    """Callees resolution placed with certainty, and whether anything was left open.

    Only *certain* answers are admitted (ADR-0002): a guess among several candidates would
    make RFC and CBO look exact while being arithmetic over hunches.

    Every name in `access.calls` is a call *through the receiver* -- that is what
    `members.py` collected -- so the receiver is passed on. Without it `resolve_call` would
    answer `self.handle()` with whatever top-level `handle` the calling file happens to
    declare, at full confidence, and RFC and CBO would count it.
    """
    if not evidence.can_resolve:
        return set(), False

    confident: set[str] = set()
    uncertain = False
    for access in model.methods.values():
        for callee in access.calls:
            found = evidence.symbols.resolve_call(  # type: ignore[union-attr]
                evidence.path, callee, access.receiver or None
            )
            if found is not None and found.is_certain:
                confident.add(found.qualified_name)
            else:
                uncertain = True
    return confident, uncertain
