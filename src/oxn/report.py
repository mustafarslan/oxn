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
