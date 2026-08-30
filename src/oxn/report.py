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
