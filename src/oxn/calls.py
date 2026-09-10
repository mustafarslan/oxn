"""The `oxn calls` surface: fan-in, fan-out, recursion and dead-code candidates.

`report.py`'s other half, split out when that file passed its `file_sloc` ceiling. The
split is along a real seam rather than a size one: everything here needs an L2 call graph
and nothing else in `report.py` does, which is why this is the only surface that can answer
`UNAVAILABLE`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from oxn.render import TO_JSON, Output, _emit_calls
from oxn.report import _indexer

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable, Mapping

    from oxn.metrics.callgraph import CallGraph


#: Why `oxn calls` will not report a dead-code total it reached without judging anything.
NO_PRIVACY = (
    "no callable in this tree could be shown private by its name. OXN roots everything it "
    "cannot show to be private, because a public name may be called from outside the tree "
    "-- so here that roots all of them, and the total would be zero without having looked. "
    "Java, Rust and TypeScript spell privacy with a modifier rather than with the name "
    "(`profiles.base.privacy_rules`); reading it needs the syntax node, which this surface "
    "does not keep. Fan-in, fan-out and recursion above are unaffected."
)

#: What `oxn calls` needs and cannot compute for itself.
NO_CALL_GRAPH = (
    "no call edges in the cache. OXN recovers a call graph from a SCIP index (ADR-0002): "
    "nothing at L0/L1 writes one, because a name resolved by scope rules is a candidate and "
    "not a call. Run `oxn index <path>` for a language whose indexer is installed, or "
    "`oxn index <path> --index-file <file.scip>` over an index you built, then ask again."
)


def run_calls(paths: list[str], output: Output = TO_JSON, *, limit: int = 20) -> dict[str, Any]:
    """Fan-in, fan-out, recursion and dead-code candidates over the L2 call graph.

    **Refuses rather than reports zero when there is no call graph.** An empty answer here
    and a "nothing is dead" answer are different claims, and only the first is true on a tree
    that has never ingested a SCIP index -- which is every tree by default. `_dead_code`
    refuses a second way, for a language whose privacy no name can decide.

    **What dead code still over-reports, measured rather than guessed.** A call through an
    *imported* name resolves to a SCIP document-local symbol, and a local has no cross-file
    meaning, so the edge resolves to nothing: **2,303 of OXN's 9,497 call edges, 24%**, against
    4,570 that genuinely leave the tree. Anything reached only from another file's import is
    therefore still reported. On OXN's own `src/` on 2026-09-10 that is 3 of 4 candidates --
    the fourth, `cli._version`, is real. Read a candidate as a question, not a verdict.

    Two false-positive sources have been removed and the numbers are worth keeping. Members
    the language dispatches without naming -- a constructor, `__eq__`, `__iter__` -- were
    124 of the 138 candidates on `python-httpx`, and the remaining 14 were private helpers
    those members were the only callers of: the report was 138 false positives out of 138,
    and is now 0. Calls written at module scope had no enclosing definition and were dropped
    entirely, which cost 5,718 edges on OXN's own tree and reported every `_main` invoked
    under `if __name__ == "__main__"` as dead.
    """
    from oxn.metrics.callgraph import (
        build_call_graph,
        default_roots,
        dispatched_members,
        name_privacy,
        unreachable,
    )

    read = _call_cache(paths)
    if not read.rows:
        payload: dict[str, Any] = {"status": "UNAVAILABLE", "note": NO_CALL_GRAPH}
        _emit_calls(payload, output)
        return payload

    entities = read.callables
    graph = build_call_graph(read.rows)
    privacy = name_privacy(read.every)
    dead = [
        found
        for found in unreachable(
            graph,
            entities,
            default_roots(read.every, privacy),
            reaches=_reaches(
                dispatched_members(entities, read.classes, read.parents, read.overriding),
                read.references,
            ),
        )
        if read.selected(found.file_path)
    ]
    payload = {
        "status": "OK",
        "callables": len(read.shown),
        "callers": len(graph.edges),
        "edges": sum(len(targets) for targets in graph.edges.values()),
        "declined": graph.unresolved_targets,
        "recursion": [cycle for cycle in graph.recursion_cycles()],
        "dead_code": _dead_code(dead, privacy, entities, limit),
        "fan": _fan(graph, entities, read.shown, limit),
    }
    _emit_calls(payload, output)
    return payload


def _fan(
    graph: CallGraph,
    entities: Mapping[str, tuple[str, str, str]],
    shown: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """The busiest callables under the requested paths, by total fan.

    Fan-in and fan-out are counted over the *whole* graph while the rows are drawn from
    ``shown``: a function's callers do not stop mattering because the user asked about one
    directory, and a fan-in that changed with the argument would be a different number
    wearing the same name.
    """
    ranked = sorted(shown, key=lambda key: -graph.fan_in(key) - graph.fan_out(key))
    return [
        {
            "qualified_name": entities[entity_id][0],
            "path": entities[entity_id][1],
            "fan_in": graph.fan_in(entity_id),
            "fan_out": graph.fan_out(entity_id),
        }
        for entity_id in ranked[:limit]
    ]


@dataclass(frozen=True)
class _CallCache:
    """Everything `run_calls` reads out of the store, read once while it is open."""

    rows: list[tuple[str, str | None, str, float]]
    overriding: set[str]
    #: Every entity, not only the callables: a class is a node in the reachability
    #: traversal, and a file is a root, so both must survive the trip out of the store.
    every: dict[str, tuple[str, str, str]]
    parents: dict[str, str | None]
    #: Source id -> what it mentions without calling. See `graph.rows.reference_edges`.
    references: dict[str, list[str]]
    selected: Callable[[str], bool]
    #: `every` partitioned the three ways the report reads it: what can be reported dead,
    #: what can own a dispatched member, and what the requested paths actually cover.
    callables: dict[str, tuple[str, str, str]]
    classes: dict[str, tuple[str, str, str]]
    shown: list[str]


def _call_cache(paths: list[str]) -> _CallCache:
    """Index what was asked for, then read the whole cache.

    Those are deliberately different scopes. Reachability is a whole-tree property -- the
    caller that decides whether something is dead is routinely in a directory the user did
    not name -- so the graph is always the whole cache and ``paths`` only chooses which
    rows are shown.
    """
    from oxn.config import CALLABLE_KINDS, CLASS_KINDS
    from oxn.graph.rows import call_edges, dispatch_sources, reference_edges

    targets = [Path(raw) for raw in paths]
    with _indexer() as indexer:
        indexer.index(targets)
        every: dict[str, tuple[str, str, str]] = {}
        parents: dict[str, str | None] = {}
        for path in indexer.store.known_paths():
            for entity in indexer.store.entities_for(path):
                every[entity.id] = (entity.qualified_name, entity.file_path, entity.kind.value)
                parents[entity.id] = entity.parent_id
        selected = _under(targets, indexer.root)
        callables = {key: row for key, row in every.items() if row[2] in CALLABLE_KINDS}
        return _CallCache(
            rows=call_edges(indexer.store),
            overriding=dispatch_sources(indexer.store),
            references=reference_edges(indexer.store),
            every=every,
            parents=parents,
            selected=selected,
            callables=callables,
            classes={key: row for key, row in every.items() if row[2] in CLASS_KINDS},
            shown=[key for key, row in callables.items() if selected(row[1])],
        )


def _reaches(
    dispatch: Mapping[str, list[str]], references: Mapping[str, list[str]]
) -> dict[str, list[str]]:
    """Every non-call way one entity reaches another, in one mapping.

    Two sources with the same shape and the same job, and an entity can be in both -- a class
    that dispatches a constructor and is itself named in a registry -- so they are merged
    rather than passed separately.
    """
    merged: dict[str, list[str]] = {key: list(value) for key, value in dispatch.items()}
    for key, value in references.items():
        merged.setdefault(key, []).extend(value)
    return merged


def _under(targets: list[Path], root: Path) -> Callable[[str], bool]:
    """Does a cached file path fall under one of the requested targets?

    ``paths`` chooses which rows are *shown*, never which the answer is computed from:
    reachability is a whole-tree property, and a caller living outside the requested
    directory is exactly the caller that decides whether something in it is dead.
    """
    prefixes = []
    for target in targets:
        try:
            relative = target.resolve().relative_to(root.resolve())
        except ValueError:
            continue
        prefixes.append("" if str(relative) == "." else str(relative))
    if any(prefix == "" for prefix in prefixes):
        return lambda _path: True
    return lambda path: any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes)


def _dead_code(
    dead: list[Any],
    privacy: Mapping[str, bool | None],
    entities: Mapping[str, tuple[str, str, str]],
    limit: int,
) -> dict[str, Any]:
    """The dead-code section, which refuses for any language it could not judge.

    `default_roots` roots everything it cannot show to be private, so a language whose
    privacy lives in a modifier -- Java, Rust, TypeScript outside ``#`` -- roots *every*
    callable and reaches a total of zero without having looked. That zero and "nothing is
    dead" are different claims, and only `NO_CALL_GRAPH`'s shape tells them apart.

    **Asked per language, not per tree.** Polyglot is the normal case, and a tree-wide test
    let one Python helper vouch for forty thousand lines of Java beside it -- Java's
    structural zero reported as a real answer because something else in the repository could
    be judged. A language nothing could be judged in is named in ``not_judged``; when that is
    every language present, the section refuses outright.
    """
    blind = _unjudged(privacy, entities)
    if blind and blind == _languages(entities):
        return {"status": "UNAVAILABLE", "note": NO_PRIVACY, "not_judged": sorted(blind)}
    section: dict[str, Any] = {
        "status": "OK",
        "candidates": [
            {"qualified_name": found.qualified_name, "path": found.file_path}
            for found in dead[:limit]
        ],
        "total": len(dead),
    }
    if blind:
        section["not_judged"] = sorted(blind)
        section["note"] = NO_PRIVACY
    return section


def _languages(entities: Mapping[str, tuple[str, str, str]]) -> set[str]:
    """Every language with a callable in this tree."""
    from oxn.profiles import profile_for_path

    found = (profile_for_path(row[1]) for row in entities.values())
    return {profile.name for profile in found if profile is not None}


def _unjudged(
    privacy: Mapping[str, bool | None], entities: Mapping[str, tuple[str, str, str]]
) -> set[str]:
    """Languages with callables here, not one of which could be shown private by name."""
    from oxn.profiles import profile_for_path

    judged: dict[str, bool] = {}
    for key, row in entities.items():
        profile = profile_for_path(row[1])
        if profile is not None:
            judged[profile.name] = judged.get(profile.name, False) or bool(privacy.get(key))
    return {language for language, seen in judged.items() if not seen}
