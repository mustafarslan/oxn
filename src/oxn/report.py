"""Human and machine renderings of what the engine found.

Kept apart from :mod:`oxn._app` so the same logic can serve the typer surface, the MCP
server (P9) and the hook, without any of them importing the others' dependencies.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from rich.console import Console

    from oxn.graph.indexer import Indexer


def run_parse(
    paths: list[str],
    *,
    json_output: bool = False,
    force: bool = False,
    stats_only: bool = False,
    console: Console | None = None,
) -> dict[str, Any]:
    """Index ``paths`` and render the result. Returns the payload either way."""
    from oxn.graph.indexer import Indexer

    targets = [Path(raw) for raw in paths]
    missing = [str(target) for target in targets if not target.exists()]
    if missing:
        failure: dict[str, Any] = {
            "status": "ERROR",
            "errors": dict.fromkeys(missing, "no such file or directory"),
        }
        _emit(failure, json_output, console)
        return failure

    with Indexer() as indexer:
        report = indexer.index(targets, force=force)
        payload: dict[str, Any] = {
            "status": "OK",
            "index": report.as_dict(),
            "cache": indexer.store.stats(),
        }
        if not stats_only:
            payload["files"] = _file_entries(indexer, targets)

    _emit(payload, json_output, console)
    return payload


def _file_entries(indexer: Indexer, targets: list[Path]) -> list[dict[str, Any]]:
    """The containment skeleton, per file, in source order."""
    from oxn.graph.indexer import iter_source_files

    relatives = sorted({indexer.relative(path) for path in iter_source_files(targets)})
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


def _emit(payload: dict[str, Any], json_output: bool, console: Console | None) -> None:
    if json_output or console is None:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    if payload["status"] == "ERROR":
        for path, message in payload["errors"].items():
            console.print(f"[red]{path}[/red]: {message}")
        return

    for entry in payload.get("files", []):
        console.print(f"[bold]{entry['path']}[/bold]")
        for ent in entry["entities"]:
            lines = f"L{ent['lines'][0]}-{ent['lines'][1]}"
            params = f"({', '.join(ent['parameters'])})" if "parameters" in ent else ""
            console.print(
                f"  [dim]{ent['kind']:9}[/dim] {ent['qualified_name']}{params}  [dim]{lines}[/dim]"
            )

    index = payload["index"]
    console.print(
        f"\n[green]{index['parsed']} parsed[/green], {index['cached']} cached "
        f"([bold]{index['cache_hit_ratio']:.0%}[/bold] cache hit), "
        f"{payload['cache']['entities']} entities in the graph"
    )
    if index["parse_incomplete"]:
        console.print(
            f"[yellow]{len(index['parse_incomplete'])} file(s) had parse errors[/yellow]; "
            "metrics for those will be suppressed"
        )


def run_metrics(
    paths: list[str],
    *,
    sort_by: str = "cognitive_complexity",
    limit: int = 20,
    explain: bool = False,
    json_output: bool = False,
    console: Console | None = None,
) -> dict[str, Any]:
    """Index ``paths`` and rank their entities by one metric."""
    from oxn.graph.indexer import Indexer, iter_source_files

    targets = [Path(raw) for raw in paths]
    missing = [str(target) for target in targets if not target.exists()]
    if missing:
        failure: dict[str, Any] = {
            "status": "ERROR",
            "errors": dict.fromkeys(missing, "no such file or directory"),
        }
        _emit(failure, json_output, console)
        return failure

    with Indexer() as indexer:
        indexer.index(targets)
        wanted = {indexer.relative(path) for path in iter_source_files(targets)}
        file_scoped = {"hotspot", "churn", "commits", "duplicate_lines", "authors"}
        if sort_by in file_scoped:
            # These belong to a path, not to a code entity, and history has no entity at all.
            rows = [
                {"qualified_name": path, "path": path, "value": value}
                for path, value in indexer.store.file_metrics(sort_by, limit=limit * 4)
                if path in wanted
            ][:limit]
        else:
            rows = [
                {"qualified_name": name, "path": path, "value": value}
                for name, path, value in indexer.store.worst(sort_by, limit=limit * 4)
                if path in wanted
            ][:limit]
        trail: list[str] = []
        if explain and rows:
            trail = _explain_worst(indexer, rows[0])

    payload: dict[str, Any] = {
        "status": "OK",
        "metric": sort_by,
        "scope": "file" if sort_by in file_scoped else "entity",
        "entities": rows,
    }
    if trail:
        payload["explanation"] = trail

    _emit_metrics(payload, json_output, console)
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


def _emit_metrics(payload: dict[str, Any], json_output: bool, console: Console | None) -> None:
    if json_output or console is None:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    metric = payload["metric"]
    file_scoped = payload.get("scope") == "file"
    console.print(f"[bold]Ranked by {metric}[/bold]\n")
    for row in payload["entities"]:
        value = row["value"]
        # A hotspot score lives in [0, 1]; a churn count does not. Format each so the
        # number stays readable instead of being rounded away.
        rendered = f"{value:>7.3f}" if 0 < abs(value) < 10 else f"{value:>7.0f}"
        if file_scoped:
            console.print(f"  [bold]{rendered}[/bold]  {row['path']}")
        else:
            name = row["qualified_name"].split(".")[-1]
            console.print(f"  [bold]{rendered}[/bold]  {name:<32} [dim]{row['path']}[/dim]")
    if payload.get("explanation"):
        worst = payload["entities"][0]
        console.print(
            f"\n[bold]Why {worst['qualified_name'].split('.')[-1]} scores "
            f"{worst['value']:.0f}:[/bold]"
        )
        for line in payload["explanation"]:
            console.print(f"  [dim]{line}[/dim]")


def run_volume(
    paths: list[str],
    *,
    json_output: bool = False,
    include_history: bool = True,
    console: Console | None = None,
) -> dict[str, Any]:
    """Duplication, erosion and hotspots for ``paths``."""
    from oxn.graph.indexer import Indexer
    from oxn.volume.scan import persist, scan_volume

    targets = [Path(raw) for raw in paths]
    missing = [str(target) for target in targets if not target.exists()]
    if missing:
        failure: dict[str, Any] = {
            "status": "ERROR",
            "errors": dict.fromkeys(missing, "no such file or directory"),
        }
        _emit(failure, json_output, console)
        return failure

    with Indexer() as indexer:
        indexer.index(targets)
        report = scan_volume(indexer, targets, include_history=include_history)
        persist(indexer, report)
        payload: dict[str, Any] = {"status": "OK", **report.as_dict()}

    _emit_volume(payload, json_output, console)
    return payload


def _emit_volume(payload: dict[str, Any], json_output: bool, console: Console | None) -> None:
    if json_output or console is None:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    duplication = payload["duplication"]
    erosion = payload["erosion"]
    console.print("[bold]Volume[/bold]")
    console.print(
        f"  verbosity        [bold]{duplication['verbosity']:.1%}[/bold] of tokens are duplicated"
        f"  [dim]({duplication['clone_classes']} clone classes,"
        f" {duplication['duplicate_tokens']:,} of {duplication['total_tokens']:,} tokens)[/dim]"
    )
    key = next(k for k in erosion if k.startswith("erosion_at_"))
    console.print(
        f"  erosion          [bold]{erosion[key]:.1%}[/bold] of complexity sits in the worst "
        f"{key.split('_')[-1]} of {erosion['function_count']} functions"
    )
    console.print(
        f"  gini             [bold]{erosion['gini']:.3f}[/bold] [dim](0 = evenly spread)[/dim]"
    )

    if payload.get("hotspots"):
        console.print(
            "\n[bold]Hotspots[/bold] [dim](change frequency x cognitive complexity)[/dim]"
        )
        for spot in payload["hotspots"][:10]:
            console.print(
                f"  [bold]{spot['score']:.3f}[/bold]  {spot['path']:<52}"
                f" [dim]{spot['commits']} commits, complexity {spot['complexity']:.0f}[/dim]"
            )


def run_arch(
    paths: list[str],
    *,
    granularity: str = "directory",
    show_unresolved: bool = False,
    json_output: bool = False,
    console: Console | None = None,
) -> dict[str, Any]:
    """Build the dependency graph for ``paths`` and report its architecture."""
    from pathlib import PurePosixPath

    from oxn.graph.architecture import analyse
    from oxn.graph.depgraph import build_dependency_graph, directory_component
    from oxn.graph.indexer import Indexer, iter_source_files

    targets = [Path(raw) for raw in paths]
    missing = [str(target) for target in targets if not target.exists()]
    if missing:
        failure: dict[str, Any] = {
            "status": "ERROR",
            "errors": dict.fromkeys(missing, "no such file or directory"),
        }
        _emit(failure, json_output, console)
        return failure

    def top_component(path: str) -> str:
        parts = PurePosixPath(path).parts
        return parts[0] if parts else "<root>"

    component_of = {
        "directory": directory_component,
        "file": lambda path: path,
        "top": top_component,
    }.get(granularity, directory_component)

    with Indexer() as indexer:
        indexer.index(targets)
        files = list(iter_source_files(targets))
        graph = build_dependency_graph(indexer.root, files, component_of=component_of)
        sizes = _component_sizes(indexer, graph, component_of)
        types = _component_types(indexer, component_of)
        report = analyse(
            graph.components,
            types=types,
            sizes=sizes,
            partition=graph.membership,
        )

    payload: dict[str, Any] = {
        "status": "OK",
        "imports": {
            "total": graph.import_count,
            "internal": sum(len(t) for t in graph.files.values()),
            "external": graph.external_count,
            "unresolved": len(graph.unresolved),
        },
        **report.as_dict(),
        "martin": {
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
        },
    }
    if show_unresolved:
        payload["unresolved_imports"] = [
            {"source": item.source, "specifier": item.specifier, "line": item.line}
            for item in graph.unresolved
        ]

    _emit_arch(payload, json_output, console)
    return payload


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


def _emit_arch(payload: dict[str, Any], json_output: bool, console: Console | None) -> None:
    if json_output or console is None:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    imports = payload["imports"]
    console.print(
        f"[bold]Dependency graph[/bold]  {payload['components']} components, "
        f"{imports['internal']} internal edges "
        f"[dim]({imports['total']} imports, {imports['external']} external, "
        f"{imports['unresolved']} unresolved)[/dim]\n"
    )

    lakos = payload["lakos"]
    if lakos:
        verdict = (
            "[red]worse than a balanced tree[/red]" if lakos["nccd"] > 1 else "[green]tight[/green]"
        )
        console.print(
            f"  coupling         NCCD [bold]{lakos['nccd']}[/bold] {verdict}"
            f"  [dim](CCD {lakos['ccd']}, ACD {lakos['acd']})[/dim]"
        )
    console.print(
        f"  propagation cost [bold]{payload['propagation_cost']:.1%}[/bold]"
        " [dim]of the system is reachable from the average component[/dim]"
    )
    console.print(f"  modularity       [bold]{payload['modularity']:.3f}[/bold]")

    if payload["cycles"]:
        console.print(f"\n[bold red]Cycles[/bold red] ({len(payload['cycles'])})")
        for cycle in payload["cycles"][:5]:
            console.print(f"  {cycle['size']} components: {', '.join(cycle['members'][:6])}")

    if payload["smells"]:
        console.print(f"\n[bold]Architectural smells[/bold] ({len(payload['smells'])})")
        for smell in payload["smells"][:10]:
            console.print(
                f"  [yellow]{smell['kind']:22}[/yellow] {smell['component']:<28}"
                f" [dim]{smell['detail']}[/dim]"
            )

    console.print("\n[bold]Components[/bold] [dim](Ca afferent, Ce efferent, I instability)[/dim]")
    for name, metrics in list(payload["martin"].items())[:12]:
        abstractness = (
            "  -  " if metrics["abstractness"] is None else f"{metrics['abstractness']:.2f}"
        )
        console.print(
            f"  {name:<34} Ca={metrics['afferent']:<3} Ce={metrics['efferent']:<3}"
            f" I={metrics['instability']:.2f} A={abstractness} L{metrics['level']}"
        )

    if payload.get("unresolved_imports"):
        console.print(f"\n[bold]Unresolved imports[/bold] ({len(payload['unresolved_imports'])})")
        for item in payload["unresolved_imports"][:10]:
            console.print(
                f"  {item['source']}:{item['line']} -> [yellow]{item['specifier']}[/yellow]"
            )


def run_index(
    paths: list[str],
    *,
    language: str = "python",
    index_file: str | None = None,
    json_output: bool = False,
    console: Console | None = None,
) -> dict[str, Any]:
    """Generate or ingest a SCIP index, merging its symbols into the graph."""
    import tempfile

    from oxn.graph.indexer import Indexer
    from oxn.scip.ingest import ingest_index
    from oxn.scip.runner import IndexerNotFound, run_indexer

    target = Path(paths[0]).resolve()
    if not target.exists():
        failure: dict[str, Any] = {"status": "ERROR", "errors": {str(target): "no such path"}}
        _emit(failure, json_output, console)
        return failure

    with Indexer(root=target) as indexer:
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
                        project_name=target.name,
                    )
                except IndexerNotFound as error:
                    failure = {"status": "ERROR", "errors": {language: str(error)}}
                    _emit(failure, json_output, console)
                    return failure

            report = ingest_index(indexer, scip_path)
            stats = indexer.store.edge_stats()

    payload: dict[str, Any] = {"status": "OK", "index": report.as_dict(), "edges": stats}
    _emit_index(payload, json_output, console)
    return payload


def _emit_index(payload: dict[str, Any], json_output: bool, console: Console | None) -> None:
    if json_output or console is None:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    if payload["status"] == "ERROR":
        for key, message in payload["errors"].items():
            console.print(f"[red]{key}[/red]: {message}")
        return

    report = payload["index"]
    console.print(
        f"[bold]SCIP index ingested[/bold]  {report['matched_documents']} of "
        f"{report['documents']} documents in {report['seconds']}s\n"
    )
    console.print(
        f"  definitions      [bold]{report['definition_coverage']:.1%}[/bold] of declarations "
        f"matched a symbol  [dim]({report['symbols']} symbols)[/dim]"
    )
    console.print(
        f"  call sites       [bold]{report['call_coverage']:.1%}[/bold] resolved to a target"
    )
    console.print(
        f"  edges            [bold]{report['edges']}[/bold] recovered, "
        f"{report['edge_resolution']:.1%} pointing inside the tree"
    )
    for kind in ("calls", "extends", "overrides"):
        if kind in payload["edges"]:
            console.print(
                f"    {kind:<12} {payload['edges'][kind]:>5}"
                f" [dim]({payload['edges'].get(kind + '_resolved', 0)} in-tree)[/dim]"
            )
    if report["skipped"]:
        console.print(
            f"\n[dim]skipped {len(report['skipped'])} document(s) OXN does not parse[/dim]"
        )
