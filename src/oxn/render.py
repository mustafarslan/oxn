"""Turning a report into something a person reads.

Split out of `report.py` for the ordinary reason: that module had grown past its own file
ceiling and OXN said so. But the seam is real rather than convenient -- every function here
takes a finished payload and writes it to a console, and none of them can change what is
measured. `report.py` decides *what* is true; this decides how to say it.

`rich` is imported by the caller and passed in, never imported here, so nothing on the hook's
fast path can reach the presentation layer through this module.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from rich.console import Console


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


def _emit_arch(payload: dict[str, Any], json_output: bool, console: Console | None) -> None:
    """Five sections, each its own function -- the report reads as its own table of contents."""
    if json_output or console is None:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    _arch_summary(payload, console)
    _arch_cycles(payload, console)
    _arch_smells(payload, console)
    _arch_components(payload, console)
    _arch_unresolved(payload, console)


def _arch_summary(payload: dict[str, Any], console: Console) -> None:
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


def _arch_cycles(payload: dict[str, Any], console: Console) -> None:
    if not payload["cycles"]:
        return
    console.print(f"\n[bold red]Cycles[/bold red] ({len(payload['cycles'])})")
    for cycle in payload["cycles"][:5]:
        console.print(f"  {cycle['size']} components: {', '.join(cycle['members'][:6])}")


def _arch_smells(payload: dict[str, Any], console: Console) -> None:
    if not payload["smells"]:
        return
    console.print(f"\n[bold]Architectural smells[/bold] ({len(payload['smells'])})")
    for smell in payload["smells"][:10]:
        console.print(
            f"  [yellow]{smell['kind']:22}[/yellow] {smell['component']:<28}"
            f" [dim]{smell['detail']}[/dim]"
        )


def _arch_components(payload: dict[str, Any], console: Console) -> None:
    console.print("\n[bold]Components[/bold] [dim](Ca afferent, Ce efferent, I instability)[/dim]")
    for name, metrics in list(payload["martin"].items())[:12]:
        abstractness = (
            "  -  " if metrics["abstractness"] is None else f"{metrics['abstractness']:.2f}"
        )
        console.print(
            f"  {name:<34} Ca={metrics['afferent']:<3} Ce={metrics['efferent']:<3}"
            f" I={metrics['instability']:.2f} A={abstractness} L{metrics['level']}"
        )


def _arch_unresolved(payload: dict[str, Any], console: Console) -> None:
    """An import OXN could not place is stated, never dropped: it may hide a layer violation."""
    if not payload.get("unresolved_imports"):
        return
    console.print(f"\n[bold]Unresolved imports[/bold] ({len(payload['unresolved_imports'])})")
    for item in payload["unresolved_imports"][:10]:
        console.print(f"  {item['source']}:{item['line']} -> [yellow]{item['specifier']}[/yellow]")


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


def _emit_classes(payload: dict[str, Any], json_output: bool, console: Console | None) -> None:
    if json_output or console is None:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    console.print(
        f"[bold]{payload['total_classes']} classes[/bold], ranked by {payload['sort_by']}"
        "  [dim](LCOM* 0 = cohesive, 1 = none; LCOM4 = how many classes this really is)[/dim]\n"
    )
    for row in payload["classes"]:
        star = "  -  " if row["lcom_star"] is None else f"{row['lcom_star']:.2f}"
        flag = "" if row["exactness"] == "EXACT" else " [yellow]~[/yellow]"
        console.print(
            f"  LCOM*={star} LCOM4={row['lcom4']:<2} WMC={row['wmc']:<3} CBO={row['cbo']:<2}"
            f" RFC={row['rfc']:<3} DIT={row['dit']:<2}  {row['class']:<26}"
            f" [dim]{row['path']}[/dim]{flag}"
        )
    console.print("\n[dim]~ marks a value computed from an inherited or dynamic access[/dim]")
