"""Detecting a ceiling evaded by splitting rather than simplified.

A ceiling on cognitive complexity is a ceiling on *one function*, and the cheapest way to
satisfy it is not to simplify anything. Split the branchy function into a handful of
single-use private helpers, and every per-function maximum drops below the line while the
work, and the reading cost, stay exactly where they were. Measured on a six-branch router
here: cognitive complexity 28 in one function became a maximum of 3 across eight, and the
gate passed it without complaint.

**What this module gates, and what it does not.** Splitting code up is usually good, and a
rule that punished decomposition would be worse than no rule. So the trigger is not "many
small functions" -- that is `store.py`, `render.py`, and half of httpx, all measured and all
innocent. The trigger is narrower and tied directly to the thing being evaded:

    a function, plus the *dedicated* helpers it alone calls, together carrying more
    complexity than the ceiling allows for one unit of work.

"Dedicated" is the discriminator: private, trivial, and called exactly once in the file. A
helper with a second caller is shared code. A helper with a body is a unit of work. A
public name might be called from anywhere, so it cannot be shown to be dedicated at all.
Those three conditions are what separate a shred from a decomposition, and each one was put
there by a measured false positive rather than by taste.

**Why the ceiling decides.** The shape test (`MANY_HELPERS`, `TRIVIAL_HELPER`) only selects
candidates; it never blocks on its own. What blocks is the cluster total against the same
`cognitive_complexity` ceiling every function already faces. That ties the rule to what is
actually being gamed and keeps it from having an opinion about style: a sequential renderer
split into five trivial sections totals 8 and passes, because 8 was never a violation. The
same split of a 28-point router totals 13 and does not.

**Its blind spot, stated plainly.** The cluster total is *not* the score the function would
have if inlined -- it is lower, because nesting increments evaporate under extraction
(docs/metrics.md section 10.5). A shred of a marginal violation can therefore sum to under
the ceiling and pass here. This rule catches the flagrant case; the dogfood harness's
differential rule, which can see the before-state, catches the marginal one. Neither is a
completeness claim.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from statistics import median
from typing import TYPE_CHECKING

from oxn.thresholds import MANY_HELPERS, TRIVIAL_HELPER

if TYPE_CHECKING:  # pragma: no cover
    from oxn.profiles.base import LanguageProfile

#: The metric key a cluster root is measured under. Only roots carry it, so every other
#: entity is simply absent from the ceiling check.
METRIC = "shredding_cluster"


@dataclass(frozen=True, slots=True)
class Cluster:
    """A root callable and the dedicated helpers that together carry its work."""

    total: float
    #: Helper names, worst first, so the diagnostic names what to put back.
    helpers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Unit:
    """One callable, reduced to what the shredding question needs."""

    entity_id: str
    #: The bare declared name, not the qualified one: call sites are written bare.
    name: str
    score: float
    #: Names this callable calls, in its own body only.
    calls: tuple[str, ...]
    #: Is it private to its file? Resolved where the *node* is, because four of six
    #: languages answer with a modifier rather than with the name -- see
    #: `profiles.base.declared_private`. `None` means the profile could not tell, and the
    #: rule declines rather than guessing.
    private: bool | None = None


def cluster_totals(units: list[Unit], profile: LanguageProfile) -> dict[str, Cluster]:
    """Map each cluster root's entity id to what it and its dedicated helpers carry.

    Only roots of a cluster that passes the shape test appear. An empty result is the
    normal case and means nothing was shaped like a shred.
    """
    if not any(unit.private is not None for unit in units):
        return {}  # nothing here can be shown private; see `profiles.base.declared_private`

    called = Counter(name for unit in units for name in unit.calls)
    defined = Counter(unit.name for unit in units)
    by_name = {unit.name: unit for unit in units}

    helpers = {unit.name for unit in units if _is_dedicated(unit, called, defined, profile)}
    if len(helpers) < MANY_HELPERS:
        return {}

    caller_of = _callers(units, helpers)
    return _totals(_group(caller_of, helpers, by_name), by_name)


def _is_dedicated(
    unit: Unit, called: Counter[str], defined: Counter[str], profile: LanguageProfile
) -> bool:
    """Does this callable exist only to hold part of one other callable's body?

    All three conditions are load-bearing, and each was put here by a measured false
    positive: a second caller means shared code, a body means a unit of work, and a public
    name cannot be shown to be dedicated at all.

    A name defined twice cannot be attributed to one definition, so it is not *provably*
    dedicated. Declining there fails safe -- the rule does not fire.
    """
    del profile
    return (
        defined[unit.name] == 1
        and called[unit.name] == 1
        and unit.score <= TRIVIAL_HELPER
        and bool(unit.private)
    )


def _callers(units: list[Unit], helpers: set[str]) -> dict[str, str]:
    """Helper name -> the name of the single callable that calls it."""
    found: dict[str, str] = {}
    for unit in units:
        for name in set(unit.calls) & helpers:
            # Self-recursion would make a helper its own root; it is not a dedicated helper.
            if name != unit.name:
                found[name] = unit.name
    return found


def _group(
    caller_of: dict[str, str], helpers: set[str], by_name: dict[str, Unit]
) -> dict[str, list[str]]:
    """Fold each helper onto the root it ultimately serves.

    Folding is transitive because shredding nests: the router split into `_get` and `_post`
    which each split again is one act, and counting only direct children would score it as
    three small clusters instead of one large one.
    """
    clusters: dict[str, list[str]] = {}
    for name in caller_of:
        root = _root(name, caller_of, helpers)
        if root is not None and root in by_name:
            clusters.setdefault(root, []).append(name)
    return clusters


def _root(name: str, caller_of: dict[str, str], helpers: set[str]) -> str | None:
    """Walk up the dedicated-helper chain to the first caller that is not itself one."""
    seen = {name}
    current = caller_of.get(name)
    while current in helpers and current not in seen:
        seen.add(current)
        current = caller_of.get(current)
    return None if current in helpers else current


def _totals(clusters: dict[str, list[str]], by_name: dict[str, Unit]) -> dict[str, Cluster]:
    """Apply the shape test, then total what survives."""
    found: dict[str, Cluster] = {}
    for root, names in clusters.items():
        scores = [by_name[name].score for name in names]
        if len(scores) < MANY_HELPERS or median(scores) > TRIVIAL_HELPER:
            continue
        ranked = sorted(names, key=lambda name: -by_name[name].score)
        found[by_name[root].entity_id] = Cluster(
            total=by_name[root].score + sum(scores), helpers=tuple(ranked)
        )
    return found
