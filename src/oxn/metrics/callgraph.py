"""Metrics over the call graph: fan-in, fan-out, recursion cycles and dead code.

Henry & Kafura (*IEEE TSE* SE-7(5), 1981) defined fan-in and fan-out via *information flow*
-- parameters, globals, return values -- not merely calls. The near-universal
"count the calls" reading is a simplification, criticised by Shepperd (1988). OXN uses the
call-graph reading and says so rather than implying otherwise.

**Only confident edges participate.** A low-confidence guess would invent a call cycle, and
a phantom cycle inflates every cognitive-complexity score in it -- corrupting the metric
this project measures most carefully. Edges from L2, or from L1 where the target was
certain, are the only ones admitted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from oxn.graph.algos import cycles

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable, Mapping


@dataclass
class CallGraph:
    """A resolved call graph over entity ids."""

    edges: dict[str, set[str]] = field(default_factory=dict)
    #: Entities named as call targets that could not be resolved inside the tree.
    unresolved_targets: int = 0

    def add(self, source: str, target: str) -> None:
        self.edges.setdefault(source, set()).add(target)
        self.edges.setdefault(target, set())

    def fan_out(self, entity_id: str) -> int:
        return len(self.edges.get(entity_id, ()))

    def fan_in(self, entity_id: str) -> int:
        return sum(1 for targets in self.edges.values() if entity_id in targets)

    def recursion_cycles(self) -> list[list[str]]:
        """Every set of entities that can reach itself -- direct or mutual recursion.

        The Cognitive Complexity specification increments for "each method in a recursion
        cycle, whether direct or indirect", so this is what upgrades that increment from the
        direct-only approximation.
        """
        return cycles(self.edges)

    def in_recursion(self) -> set[str]:
        return {member for cycle in self.recursion_cycles() for member in cycle}


def build_call_graph(
    edges: Iterable[tuple[str, str | None, str, float]],
) -> CallGraph:
    """Assemble a call graph from ``(source, target, resolution, confidence)`` rows.

    Rows whose target is unknown, or whose confidence is below certainty at L0/L1, are
    counted rather than admitted -- see the module docstring for why.
    """
    graph = CallGraph()
    for source, target, resolution, confidence in edges:
        if target is None:
            graph.unresolved_targets += 1
            continue
        if resolution != "L2" and confidence < 1.0:
            graph.unresolved_targets += 1
            continue
        graph.add(source, target)
    return graph


@dataclass(frozen=True, slots=True)
class DeadCodeCandidate:
    """An entity nothing reaches from a declared root.

    Deliberately a *candidate*. Reflection, dependency injection, framework entry points and
    dynamic dispatch all make false positives inevitable, so this never blocks -- a gate
    that deletes code on this signal is dangerous.
    """

    entity_id: str
    qualified_name: str
    file_path: str
    kind: str


def unreachable(
    graph: CallGraph,
    entities: Mapping[str, tuple[str, str, str]],
    roots: Iterable[str],
    *,
    reaches: Mapping[str, Iterable[str]] | None = None,
) -> list[DeadCodeCandidate]:
    """Entities not reachable from ``roots``.

    ``entities`` maps id to ``(qualified name, file path, kind)``, and only its members are
    *reported*. Reachability, though, runs over the whole graph: a class is a node here even
    though a class is not a candidate, because writing ``Client(...)`` is how the language
    reaches ``Client.__init__``.

    ``reaches`` is every way one entity reaches another that is *not* a call: a class to the
    members its language dispatches (`dispatched_members`), and an entity to what it merely
    mentions (`graph.rows.reference_edges`). Kept out of ``graph.edges`` rather than merged
    into it, because fan-in, fan-out and the recursion increment are counts of **calls** --
    a constructor nobody writes a call for, or a function named in a handler table, has not
    been called once and must not read as though it had.
    """
    reached: set[str] = set()
    reaches = reaches or {}
    # Seeded unfiltered: a root may be a class, which is a node here but not a candidate.
    frontier = list(roots)
    while frontier:
        current = frontier.pop()
        if current in reached:
            continue
        reached.add(current)
        frontier.extend(graph.edges.get(current, ()))
        frontier.extend(reaches.get(current, ()))

    return sorted(
        (
            DeadCodeCandidate(entity_id, name, path, kind)
            for entity_id, (name, path, kind) in entities.items()
            if entity_id not in reached
        ),
        key=lambda candidate: (candidate.file_path, candidate.qualified_name),
    )


def name_privacy(entities: Mapping[str, tuple[str, str, str]]) -> dict[str, bool | None]:
    """Is each entity private, judged from its declared name and its language?

    ``None`` where the language cannot say by name -- Java spells privacy with a modifier,
    Rust with the absence of ``pub``, TypeScript with either -- and `LanguageProfile.is_private`
    declines rather than guessing. A caller must treat ``None`` as "may be called from
    outside"; see `default_roots`.
    """
    from oxn.profiles import profile_for_path

    privacy: dict[str, bool | None] = {}
    for entity_id, (qualified_name, path, _) in entities.items():
        profile = profile_for_path(path)
        leaf = qualified_name.rsplit(".", 1)[-1]
        privacy[entity_id] = profile.is_private(leaf) if profile is not None else None
    return privacy


def dispatched_members(
    entities: Mapping[str, tuple[str, str, str]],
    owners: Mapping[str, tuple[str, str, str]],
    parents: Mapping[str, str | None],
    overriding: Iterable[str] = (),
) -> dict[str, list[str]]:
    """Class id -> the members its language reaches without a call site.

    Two sources, because languages split on this. A *name* rule covers what the grammar
    dispatches -- Python's dunders, a TypeScript ``constructor``, a Java constructor named
    as its class. An ``OVERRIDES`` edge covers the rest: Go satisfies an interface and Rust
    implements a trait without either being visible in the member's own name, and SCIP has
    already established that relation, so it is read rather than inferred.

    These attach to the owning class rather than becoming roots outright, so that a class
    nobody ever constructs stays reportable -- which is the whole reason a class is a node
    in `unreachable`'s traversal and not merely a container.
    """
    dispatched = set(overriding)
    for entity_id, row in entities.items():
        if _dispatches(entity_id, row, owners, parents):
            dispatched.add(entity_id)

    by_owner: dict[str, list[str]] = {}
    for entity_id in dispatched:
        parent = parents.get(entity_id)
        if parent is not None:
            by_owner.setdefault(parent, []).append(entity_id)
    return by_owner


def _dispatches(
    entity_id: str,
    row: tuple[str, str, str],
    owners: Mapping[str, tuple[str, str, str]],
    parents: Mapping[str, str | None],
) -> bool:
    """Does this entity's language invoke it by name rule alone? See `dispatched_members`."""
    from oxn.profiles import profile_for_path
    from oxn.profiles.base import dispatched_by_name

    qualified_name, path, kind = row
    profile = profile_for_path(path)
    if profile is None or kind not in {"method", "function"}:
        return False
    owner = owners.get(parents.get(entity_id) or "")
    owner_name = owner[0].rsplit(".", 1)[-1] if owner else None
    return dispatched_by_name(qualified_name.rsplit(".", 1)[-1], owner_name, profile)


def default_roots(
    entities: Mapping[str, tuple[str, str, str]],
    privacy: Mapping[str, bool | None] | None = None,
    *,
    test_prefixes: tuple[str, ...] = ("test_",),
) -> set[str]:
    """Entry points every project has, before any configured ones.

    ``main``, tests, and anything not private -- a test is a root because a test runner
    calls it, and treating tests as dead code would bury the report in noise.

    **"Not private" is a library's assumption, and it is the safe one.** A public method
    with no in-tree caller may be the API someone else imports, so it is rooted and never
    reported. On an application that is a false-negative machine: a public method nothing
    calls is invisible here. That direction is deliberate -- a dead-code report that is
    wrong deletes working code -- and it is why `run_calls` refuses to report a zero it
    reached because *nothing* could be judged private.

    ``privacy`` maps id to `name_privacy`'s answer. ``None`` there means the language could
    not tell from the name, and is read as public.
    """
    from oxn.config import FILE_KINDS

    privacy = name_privacy(entities) if privacy is None else privacy
    roots: set[str] = set()
    for entity_id, (qualified_name, path, kind) in entities.items():
        leaf = qualified_name.rsplit(".", 1)[-1]
        if (
            # A file body runs whenever the file is imported, whatever the file is named,
            # so it is the one root that privacy has no say over.
            kind in FILE_KINDS
            or leaf in {"main", "__main__"}
            or leaf.startswith(test_prefixes)
            or "/test" in path
            or path.startswith("test")
            or not privacy.get(entity_id)
        ):
            roots.add(entity_id)
    return roots
