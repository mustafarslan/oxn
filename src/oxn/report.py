"""Human and machine renderings of what the engine found.

Kept apart from :mod:`oxn._app` so the same logic can serve the typer surface, the MCP
server (P9) and the hook, without any of them importing the others' dependencies.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from oxn.render import (
    TO_JSON,
    Output,
    _emit,
    _emit_arch,
    _emit_classes,
    _emit_index,
    _emit_metrics,
    _emit_review,
    _emit_volume,
)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

    from oxn.graph.indexer import Indexer


def _indexer() -> Indexer:
    """An indexer that honours `oxn.yaml`'s `exclude`, like the gate does.

    The report path is not a gate, so it reads no ceilings -- but *which files are ours to
    measure* is the same question here as there. A metrics report that ranks vendored or
    generated code alongside hand-written code is the noise `docs/metrics.md` warns about,
    and a project that has already declared that boundary should not have to declare it
    twice.
    """
    from oxn.config import Config
    from oxn.graph.indexer import Indexer as _Indexer
    from oxn.graph.store import DEFAULT_CACHE_PATH

    settings = Config.load()
    return _Indexer(
        root=settings.root,
        cache_path=settings.root / DEFAULT_CACHE_PATH,
        exclude=settings.exclude,
    )


def run_parse(
    paths: list[str],
    output: Output = TO_JSON,
    *,
    force: bool = False,
    stats_only: bool = False,
) -> dict[str, Any]:
    """Index ``paths`` and render the result. Returns the payload either way."""

    targets = [Path(raw) for raw in paths]
    missing = [str(target) for target in targets if not target.exists()]
    if missing:
        failure: dict[str, Any] = {
            "status": "ERROR",
            "errors": dict.fromkeys(missing, "no such file or directory"),
        }
        _emit(failure, output)
        return failure

    with _indexer() as indexer:
        report = indexer.index(targets, force=force)
        payload: dict[str, Any] = {
            "status": "OK",
            "index": report.as_dict(),
            "cache": indexer.store.stats(),
        }
        if not stats_only:
            payload["files"] = _file_entries(indexer, targets)

    _emit(payload, output)
    return payload


def _file_entries(indexer: Indexer, targets: list[Path]) -> list[dict[str, Any]]:
    """The containment skeleton, per file, in source order."""
    relatives = sorted({indexer.relative(path) for path in indexer.sources(targets)})
    entries: list[dict[str, Any]] = []
    for rel in relatives:
        entities: list[dict[str, Any]] = []
        for entity in indexer.store.entities_for(rel):
            record: dict[str, Any] = {
                "kind": entity.kind.value,
                "qualified_name": entity.qualified_name,
                "lines": [entity.start_line, entity.end_line],
            }
            params = entity.attrs.get("parameters")
            if params:
                record["parameters"] = params
            if entity.attrs.get("is_abstract"):
                record["is_abstract"] = True
            entities.append(record)
        entries.append({"path": rel, "entities": entities})
    return entries


#: Metrics that belong to a path rather than to a code entity. History has no entity at all.
_FILE_SCOPED = frozenset({"hotspot", "churn", "commits", "duplicate_lines", "authors"})


def _ranked(indexer: Indexer, sort_by: str, limit: int, wanted: set[str]) -> list[dict[str, Any]]:
    """The worst `limit` rows for one metric, narrowed to `wanted` *before* the limit.

    Narrowing in the store rather than afterwards is the whole point: ranking the graph and
    filtering the result asked which of the *project's* worst entities happened to fall in
    these files, so a file healthier than the project's worst reported nothing at all --
    with `status: OK`, which reads as a clean bill.
    """
    if sort_by in _FILE_SCOPED:
        return [
            {"qualified_name": path, "path": path, "value": value}
            for path, value in indexer.store.file_metrics(sort_by, limit=limit, paths=wanted)
        ]
    return [
        {"qualified_name": name, "path": path, "value": value}
        for name, path, value in indexer.store.worst(sort_by, limit=limit, paths=wanted)
    ]


def metrics_payload(
    paths: list[str],
    *,
    sort_by: str = "cognitive_complexity",
    limit: int = 20,
    explain: bool = False,
) -> dict[str, Any]:
    """Index ``paths`` and rank their entities by one metric. Writes nothing.

    Split from :func:`run_metrics` for the MCP server, which wants the payload and not the
    rendering: stdout is the wire there, so a surface that prints is a surface the server
    cannot call. Every ``run_*`` in this module has the same shape, and this is the first
    one a second caller needed -- the others follow when they get one.
    """
    targets = [Path(raw) for raw in paths]
    missing = [str(target) for target in targets if not target.exists()]
    if missing:
        return {"status": "ERROR", "errors": dict.fromkeys(missing, "no such file or directory")}

    with _indexer() as indexer:
        indexer.index(targets)
        wanted = {indexer.relative(path) for path in indexer.sources(targets)}
        rows = _ranked(indexer, sort_by, limit, wanted)
        trail = _explain_worst(indexer, rows[0]) if explain and rows else []

    payload: dict[str, Any] = {
        "status": "OK",
        "metric": sort_by,
        "scope": "file" if sort_by in _FILE_SCOPED else "entity",
        "entities": rows,
    }
    if trail:
        payload["explanation"] = trail
    return payload


def run_metrics(
    paths: list[str],
    output: Output = TO_JSON,
    *,
    sort_by: str = "cognitive_complexity",
    limit: int = 20,
    explain: bool = False,
) -> dict[str, Any]:
    """Index ``paths``, rank their entities by one metric, and render the result."""
    payload = metrics_payload(paths, sort_by=sort_by, limit=limit, explain=explain)
    if payload["status"] == "ERROR":
        _emit(payload, output)
        return payload
    _emit_metrics(payload, output)
    return payload


def _explain_worst(indexer: Any, row: dict[str, Any]) -> list[str]:
    """Re-measure the worst offender to recover its increment trail."""
    import json as _json

    cursor = indexer.store._conn.execute(  # noqa: SLF001 -- reporting reads the cache directly
        "SELECT m.explanation FROM metrics m JOIN entities e ON e.id = m.entity_id"
        " WHERE e.qualified_name = ? AND m.file_path = ? AND m.metric_key = 'cognitive_complexity'",
        (row["qualified_name"], row["path"]),
    ).fetchone()
    if cursor is None or cursor["explanation"] is None:
        return []
    return list(_json.loads(cursor["explanation"]))


def run_volume(
    paths: list[str],
    output: Output = TO_JSON,
    *,
    include_history: bool = True,
) -> dict[str, Any]:
    """Duplication, erosion and hotspots for ``paths``."""
    from oxn.volume.scan import persist, scan_volume

    targets = [Path(raw) for raw in paths]
    missing = [str(target) for target in targets if not target.exists()]
    if missing:
        failure: dict[str, Any] = {
            "status": "ERROR",
            "errors": dict.fromkeys(missing, "no such file or directory"),
        }
        _emit(failure, output)
        return failure

    with _indexer() as indexer:
        indexer.index(targets)
        report = scan_volume(indexer, targets, include_history=include_history)
        persist(indexer, report)
        payload: dict[str, Any] = {"status": "OK", **report.as_dict()}

    _emit_volume(payload, output)
    return payload


def _top_component(path: str) -> str:
    """The first path segment, which is the coarsest useful component boundary."""
    from pathlib import PurePosixPath

    parts = PurePosixPath(path).parts
    return parts[0] if parts else "<root>"


def _directory_component(path: str) -> str:
    from oxn.graph.depgraph import directory_component

    return directory_component(path)


#: How `--by` maps a file to the component it belongs to.
_GRANULARITIES: dict[str, Callable[[str], str]] = {
    "directory": _directory_component,
    "file": lambda path: path,
    "top": _top_component,
}


def _arch_payload(graph: Any, report: Any, *, show_unresolved: bool) -> dict[str, Any]:
    """The reported shape of one dependency graph."""
    payload: dict[str, Any] = {
        "status": "OK",
        "imports": {
            "total": graph.import_count,
            "internal": sum(len(targets) for targets in graph.files.values()),
            "external": graph.external_count,
            "unresolved": len(graph.unresolved),
        },
        **report.as_dict(),
        "martin": _martin_rows(report),
    }
    if show_unresolved:
        payload["unresolved_imports"] = [
            {"source": item.source, "specifier": item.specifier, "line": item.line}
            for item in graph.unresolved
        ]
    return payload


def run_arch(
    paths: list[str],
    output: Output = TO_JSON,
    *,
    granularity: str = "directory",
    show_unresolved: bool = False,
) -> dict[str, Any]:
    """Build the dependency graph for ``paths`` and report its architecture."""
    from oxn.graph.architecture import analyse
    from oxn.graph.depgraph import build_dependency_graph

    targets = [Path(raw) for raw in paths]
    missing = [str(target) for target in targets if not target.exists()]
    if missing:
        failure: dict[str, Any] = {
            "status": "ERROR",
            "errors": dict.fromkeys(missing, "no such file or directory"),
        }
        _emit(failure, output)
        return failure

    component_of = _GRANULARITIES.get(granularity)
    if component_of is None:
        # Silently falling back to the default would hide a typo behind plausible output.
        failure = {
            "status": "ERROR",
            "errors": {
                granularity: f"unknown granularity; choose one of {', '.join(_GRANULARITIES)}"
            },
        }
        _emit(failure, output)
        return failure

    with _indexer() as indexer:
        indexer.index(targets)
        files = indexer.sources(targets)
        graph = build_dependency_graph(indexer.root, files, component_of=component_of)
        sizes = _component_sizes(indexer, graph, component_of)
        types = _component_types(indexer, component_of)
        report = analyse(
            graph.components,
            types=types,
            sizes=sizes,
            partition=graph.membership,
            # Cycles only: an import Python or CommonJS defers to call time is real
            # coupling and belongs in every other metric here, but it is not an
            # initialisation-order edge, and calling it a ring inverts the remedy.
            initialisation=graph.hard_components,
        )

    payload = _arch_payload(graph, report, show_unresolved=show_unresolved)

    _emit_arch(payload, output)
    return payload


def _martin_rows(report: Any) -> dict[str, dict[str, Any]]:
    """Martin's metrics per component, rounded for display.

    `abstractness` and `distance` stay `None` rather than becoming zero where a language has
    no notion of an abstract type: a missing measurement and a measurement of zero mean
    different things, and only one of them should be plotted.
    """
    return {
        name: {
            "afferent": metrics.afferent,
            "efferent": metrics.efferent,
            "instability": round(metrics.instability, 3),
            "abstractness": None
            if metrics.abstractness is None
            else round(metrics.abstractness, 3),
            "distance": None if metrics.distance is None else round(metrics.distance, 3),
            "level": report.levels.get(name, 0),
        }
        for name, metrics in sorted(report.martin.items())
    }


def _component_sizes(indexer: Any, graph: Any, component_of: Any) -> dict[str, int]:
    """Lines of code per component, for the God Component smell."""
    sizes: dict[str, int] = {}
    for path in graph.files:
        for entity in indexer.store.entities_for(path):
            if entity.parent_id is None:
                sizes[component_of(path)] = sizes.get(component_of(path), 0) + entity.line_count
                break
    return sizes


def _component_types(indexer: Any, component_of: Any) -> dict[str, tuple[int, int]]:
    """(abstract, total) type counts per component, for Martin's abstractness."""
    from oxn.graph.depgraph import component_type_counts

    rows = indexer.store._conn.execute(  # noqa: SLF001 -- reporting reads the cache directly
        "SELECT file_path, kind, attrs FROM entities WHERE kind IN ('class', 'interface')"
    ).fetchall()
    triples = [
        (row["file_path"], row["kind"], '"is_abstract": true' in row["attrs"]) for row in rows
    ]
    return component_type_counts(triples, component_of)


def run_index(
    paths: list[str],
    output: Output = TO_JSON,
    *,
    language: str = "python",
    index_file: str | None = None,
) -> dict[str, Any]:
    """Generate or ingest a SCIP index, merging its symbols into the graph."""
    import tempfile

    from oxn.config import Config
    from oxn.graph.indexer import Indexer
    from oxn.scip.ingest import ingest_index
    from oxn.scip.runner import IndexerNotFound, Project, run_indexer

    target = Path(paths[0]).resolve()
    if not target.exists():
        failure: dict[str, Any] = {"status": "ERROR", "errors": {str(target): "no such path"}}
        _emit(failure, output)
        return failure

    # `Config.load(target)`, not `_indexer()`: `oxn index <dir>` roots the cache at the
    # directory it was pointed at -- indexing a corpus must not write into the caller's
    # cache -- but it must still read that directory's own `exclude`. Bypassing it wrote
    # 1,464 excluded files into OXN's cache, which `_indexer`'s docstring says is the whole
    # reason it exists and which nothing else in the index path repeated.
    settings = Config.load(target)
    with Indexer(root=target, exclude=settings.exclude) as indexer:
        scip_path: Path
        with tempfile.TemporaryDirectory() as scratch:
            if index_file:
                scip_path = Path(index_file)
            else:
                try:
                    scip_path = run_indexer(
                        language,
                        target,
                        Path(scratch) / "index.scip",
                        Project(name=target.name),
                    )
                except IndexerNotFound as error:
                    failure = {"status": "ERROR", "errors": {language: str(error)}}
                    _emit(failure, output)
                    return failure

            report = ingest_index(indexer, scip_path)
            stats = indexer.store.edge_stats()

    payload: dict[str, Any] = {"status": "OK", "index": report.as_dict(), "edges": stats}
    _emit_index(payload, output)
    return payload


def run_classes(
    paths: list[str],
    output: Output = TO_JSON,
    *,
    sort_by: str = "lcom_star",
    limit: int = 20,
) -> dict[str, Any]:
    """Cohesion and coupling for every class under ``paths``."""
    from oxn.graph.depgraph import build_dependency_graph
    from oxn.metrics.cohesion import cohesion
    from oxn.metrics.coupling import Evidence, build_hierarchy, ck_metrics
    from oxn.resolve.project import build_class_views
    from oxn.resolve.symbols import build_project_symbols

    targets = [Path(raw) for raw in paths]
    missing = [str(target) for target in targets if not target.exists()]
    if missing:
        failure: dict[str, Any] = {
            "status": "ERROR",
            "errors": dict.fromkeys(missing, "no such file or directory"),
        }
        _emit(failure, output)
        return failure

    with _indexer() as indexer:
        files = indexer.sources(targets)
        views = build_class_views(indexer, files)
        graph = build_dependency_graph(indexer.root, files)
        symbols = build_project_symbols(views.entities, views.scopes, graph)
        hierarchy = build_hierarchy(views.models)

        rows: list[dict[str, Any]] = []
        for relative, models in views.models.items():
            for model in models.values():
                measures = cohesion(model)
                ck = ck_metrics(
                    model,
                    hierarchy,
                    Evidence(
                        path=relative,
                        symbols=symbols,
                        complexity=views.weights[relative].get(model.name, {}),
                    ),
                )
                rows.append({"path": relative, **measures.as_dict(), **ck.as_dict()})

    rows.sort(key=lambda row: (row.get(sort_by) is None, -(row.get(sort_by) or 0)))
    payload: dict[str, Any] = {
        "status": "OK",
        "sort_by": sort_by,
        "classes": rows[:limit],
        "total_classes": len(rows),
    }
    _emit_classes(payload, output)
    return payload


#: Named by the roadmap, not implemented: see the `writers` module docstring on why a client
#: that has never made a request is not evidence of support.
UNKNOWN_WRITER = (
    "no writer backend named {name!r}. `ollama` is the one implemented, because it is the one "
    "this repository can run and measure. Anthropic, OpenAI and Gemini slot into the same "
    "`Writer` protocol in `oxn.writers`; shipping them untested would claim four providers "
    "where there is evidence for one."
)


def run_review_report(
    base: str,
    head: str,
    output: Output = TO_JSON,
    *,
    write: str = "",
    model: str = "",
) -> dict[str, Any]:
    """`oxn review`: the measurement, and the comment when one was asked for.

    The comment is a *second* key rather than a replacement for the measurement. A reviewer
    who wants to check a sentence should not have to re-run the tool to see what it was based
    on, and a refused comment is only legible beside the numbers it failed to quote.
    """
    from oxn.review import run_review
    from oxn.writers import review_body

    payload = run_review(base, head)
    payload["comment"] = _comment(review_body(payload), payload, write=write, model=model)
    _emit_review(payload, output)
    return payload


def _comment(mechanical: str, payload: dict[str, Any], *, write: str, model: str) -> dict[str, Any]:
    """What to post, who wrote it, and what a model said that cost it the chance.

    **`body` is always populated.** Without `--write` it is OXN's own summary; with a working
    writer it is the model's prose; and when the model states a number the measurement does
    not contain it is OXN's own summary again, with `invented` naming the numbers and `status`
    saying `REFUSED` so nobody reads the fallback as an endorsement.

    That is a narrowing of what refusing used to mean here, and the narrower reading is the
    right one. The rule is that *prose nobody audits may not carry an unmeasured number* -- not
    that a pull request deserves no answer because a model misbehaved. The measurement was
    already made; withholding it punishes the author for the writer's fault.
    """
    if not write:
        return {"status": "OK", "body": mechanical, "model": "oxn", "invented": [], "attempts": 0}
    if write != "ollama":
        note = UNKNOWN_WRITER.format(name=write)
        return {"status": "REFUSED", "body": mechanical, "model": "oxn", "note": note}

    from oxn.writers import ollama_writer, write_review

    written = write_review(payload, ollama_writer(model)).as_dict()
    return written if written["status"] == "OK" else {**written, "body": mechanical}
