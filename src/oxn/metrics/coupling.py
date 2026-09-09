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

from oxn.graph.model import EntityKind

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping, Sequence

    from oxn.graph.model import Entity, EntityMetrics
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


def method_weights(measured: Sequence[EntityMetrics]) -> dict[str, dict[str, int]]:
    """Class name -> method name -> cyclomatic complexity, ready for `Evidence.complexity`.

    `docs/metrics.md` section 3.6 specifies cyclomatic as OXN's default WMC weight, and
    without it `ck_metrics` falls back to `sum(1 for ...)`: **WMC was NOM under another
    name**, identical in every class of every corpus measured. The hook existed from the
    start and nothing ever filled it.

    Keyed by the *bare* names `ClassModel` uses, which is why the qualified name is split
    rather than matched: `m.Router.route` contributes `Router -> route`. Two classes sharing
    a bare name in one file collide here exactly as they already collide in `models`.
    """
    weights: dict[str, dict[str, int]] = {}
    for entity in measured:
        if entity.kind is not EntityKind.METHOD:
            continue
        parts = entity.qualified_name.split(".")
        if len(parts) < 2:
            continue
        value = entity.values.get("cyclomatic_complexity")
        if value is not None:
            weights.setdefault(parts[-2], {})[parts[-1]] = int(value.value)
    return weights


#: Entities that can own methods.
_OWNER_KINDS = frozenset({EntityKind.CLASS, EntityKind.INTERFACE})


def package_class_totals(
    entities: Sequence[Entity], cyclomatic: Mapping[str, float]
) -> tuple[dict[str, tuple[int, float]], dict[str, str]]:
    """Class entity id -> (NOM, WMC) over a whole package, not one file.

    Returns the totals and, separately, the member-holding blocks that were folded into a
    named type. The caller needs both: a folded block's own per-file `nom` and `wmc` are now
    a fraction of its type's and have to be dropped rather than left to disagree.

    Every language OXN supports except Go nests a method inside its type's body, so
    containment settles ownership and `metrics.engine` can answer per file. Go declares a
    method at file scope with a receiver -- `func (c *Counter) Inc()` -- so the type may be
    declared in a sibling file and one tree cannot see it.

    **The join is by package, and that is a language guarantee rather than a heuristic.** Go
    requires a method to be declared in the same package as its receiver type, and a package
    is a directory. Measured on go-kit, all 376 methods sit in the same *file* as their type
    and none crosses the test/non-test boundary, so package scope costs nothing today -- but
    a file-scoped answer would change value when a package was split across files, and an
    aggregate whose value depends on which file the gate happened to measure cannot be
    compared against a baseline. The ratchet is what makes that fatal rather than untidy.
    """
    by_name, totals = _declared_types(entities)
    fragments = merged_fragments(entities, by_name)
    for entity in entities:
        carried = totals.get(_owner_of(entity, by_name, fragments) or "")
        if carried is not None:
            carried[0] += 1
            carried[1] += cyclomatic.get(entity.id, 0.0)
    return {
        owner: (int(count), weight)
        for owner, (count, weight) in totals.items()
        if owner not in fragments
    }, fragments


def merged_fragments(entities: Sequence[Entity], by_name: Mapping[str, str]) -> dict[str, str]:
    """Member-holding blocks that are part of a named type -> that type's entity id.

    A Rust `impl` block is an unnamed class entity holding a named type's methods, and a type
    may have any number of them. Counted per block, `Wide` with seven methods in each of two
    `impl`s read NOM 7 and walked through a ceiling of 12 that the same fourteen methods in
    one block failed -- an evasion needing no renaming and no indirection, in the language's
    most ordinary idiom.

    Only blocks naming a type this package declares. A block whose type is elsewhere keeps
    its own count, which is a lower bound rather than a zero: the alternative is a type that
    silently stops being measured because its `impl` moved.
    """
    found: dict[str, str] = {}
    for entity in entities:
        implements = entity.attrs.get("implements")
        owner = by_name.get(str(implements)) if implements else None
        if owner is not None and owner != entity.id:
            found[entity.id] = owner
    return found


def _declared_types(
    entities: Sequence[Entity],
) -> tuple[dict[str, str], dict[str, list[float]]]:
    """Every type this package declares, indexed by name and seeded with empty totals.

    First declaration wins a duplicated name. Two types with one name in a package does not
    compile in Go, so the case is a parse artifact rather than something to resolve.
    """
    by_name: dict[str, str] = {}
    totals: dict[str, list[float]] = {}
    for entity in entities:
        if entity.kind in _OWNER_KINDS:
            totals[entity.id] = [0, 0.0]
            if entity.name:
                by_name.setdefault(entity.name, entity.id)
    return by_name, totals


def _owner_of(
    entity: Entity, by_name: Mapping[str, str], fragments: Mapping[str, str]
) -> str | None:
    """The type a method belongs to: the one its receiver names, else the one it sits in.

    Receiver first, because only a language that declares methods outside the body has one,
    and there it is the *only* evidence -- containment puts the method in its file's module.

    Containment then answers with the enclosing *block*, which in Rust is an `impl` and not
    the type, so `fragments` carries the last step. Without it a type's methods were counted
    once per block.
    """
    if entity.kind is not EntityKind.METHOD:
        return None
    receiver = entity.attrs.get("receiver_type")
    if receiver:
        return by_name.get(str(receiver))
    parent = entity.parent_id or ""
    return fragments.get(parent, parent) or None


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
