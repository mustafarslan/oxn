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
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from rich.console import Console


@dataclass(frozen=True, slots=True)
class Output:
    """Where a report goes: a console for a person, or JSON for a machine.

    This was two parameters -- `json_output` and `console` -- that were always passed
    together and always agreed. `_app.py` computed `console=None if json_output else
    _console()` and then passed *both*, and `_emit` already treated a missing console as
    "write JSON". The pair also admitted a fourth state, JSON requested *with* a console,
    which no caller produced and no renderer had an answer for.
    """

    console: Console | None = None

    @property
    def as_json(self) -> bool:
        return self.console is None


#: The default for every `run_*`: no console, so the payload is written as JSON. A module
#: constant rather than `Output()` in a default argument, so the signature does not evaluate
#: a call and the body does not need a `None` branch to undo one.
TO_JSON = Output()


def _emit(payload: dict[str, Any], output: Output) -> None:
    console = output.console
    if console is None:
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


def _emit_metrics(payload: dict[str, Any], output: Output) -> None:
    console = output.console
    if console is None:
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


def _emit_volume(payload: dict[str, Any], output: Output) -> None:
    console = output.console
    if console is None:
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
    _volume_history(payload, console)


def _volume_history(payload: dict[str, Any], console: Console) -> None:
    """Co-change and ownership -- what the *history* says, next to what the code says.

    Both were implemented and reached no surface. The pairing is the point: a co-change pair
    with no import between them is Modularity Violation (Mo et al., TSE 47(5), 2021), and a
    file with many low-expertise contributors is the signal Bird et al. (FSE 2011) found
    correlating with pre-release failures at 0.86-0.93 on Vista and Windows 7 -- above size,
    churn, and every complexity metric Microsoft collected.
    """
    coupling = payload.get("coupling") or []
    if coupling:
        console.print("\n[bold]Change coupling[/bold] [dim](files that move together)[/dim]")
        for pair in coupling[:10]:
            console.print(
                f"  [bold]{pair['support']:3}[/bold] commits, {pair['confidence']:.0%} of them"
                f"  [dim]{pair['first']} <-> {pair['second']}[/dim]"
            )
    if payload.get("bus_factor"):
        console.print(
            f"\n[bold]Bus factor[/bold] {payload['bus_factor']}"
            "  [dim]authors owning more than half the files; counts files, not knowledge[/dim]"
        )


def _emit_calls(payload: dict[str, Any], output: Output) -> None:
    """The call graph, or the reason there isn't one.

    `UNAVAILABLE` is a first-class answer here. Every other surface in OXN works at L0/L1 and
    means something without a SCIP index; this one does not, and printing "0 dead-code
    candidates" for a tree nobody indexed would be the most confident wrong answer the tool
    could give.
    """
    console = output.console
    if console is None:
        _emit(payload, output)
        return
    if payload["status"] != "OK":
        console.print(f"[yellow]No call graph.[/yellow] [dim]{payload['note']}[/dim]")
        return

    console.print(
        f"[bold]Call graph[/bold] {payload['edges']} edges from {payload['callers']}"
        f" of {payload['callables']} callables"
        f"  [dim]{payload['declined']} declined: unresolved, or resolved without"
        " certainty at L0/L1[/dim]"
    )
    for row in payload["fan"][:10]:
        console.print(
            f"  in [bold]{row['fan_in']:3}[/bold] out [bold]{row['fan_out']:3}[/bold]"
            f"  {row['qualified_name']:<44} [dim]{row['path']}[/dim]"
        )
    if payload["recursion"]:
        console.print(f"\n[bold]Recursion[/bold] ({len(payload['recursion'])} cycles)")
        for cycle in payload["recursion"][:5]:
            console.print(f"  {len(cycle)} callables")
    dead = payload["dead_code"]
    if dead["status"] != "OK":
        console.print(
            f"\n[bold]Dead code[/bold]  [yellow]unavailable[/yellow]\n  [dim]{dead['note']}[/dim]"
        )
        return
    if dead["candidates"]:
        console.print(
            f"\n[bold]Dead-code candidates[/bold] ({dead['total']})"
            "  [dim]candidates: reflection, DI and framework entry points all reach code"
            " no call edge does[/dim]"
        )
        for found in dead["candidates"][:10]:
            console.print(f"  {found['qualified_name']:<48} [dim]{found['path']}[/dim]")


def _emit_arch(payload: dict[str, Any], output: Output) -> None:
    """Five sections, each its own function -- the report reads as its own table of contents."""
    console = output.console
    if console is None:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    _arch_summary(payload, console)
    _arch_cycles(payload, console)
    _arch_visibility(payload, console)
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


#: What each role means, in the order a reader should meet them.
_ROLES = (
    ("core", "red", "reaches most of the system and is reached by most of it"),
    ("shared", "yellow", "reached by many, reaches few -- a utility"),
    ("control", "cyan", "reaches many, reached by few -- an entry point"),
    ("peripheral", "green", "neither"),
)


def _arch_visibility(payload: dict[str, Any], console: Console) -> None:
    """Core/periphery, with the one caveat that decides how to read it.

    The roles are **relative to this project**: the threshold is its own largest cyclic
    group, so every system with a cycle has a core and the split says where the weight sits,
    never that a project is bad. What it is for is the cost asymmetry -- Sturtevant &
    MacCormack (JSS 120, 2016) measured a line in a central file costing over 15x as much
    per year to maintain as one on the periphery.
    """
    visibility = payload.get("visibility") or {}
    if not visibility:
        return
    counts = Counter(seen["role"] for seen in visibility.values())
    console.print(f"\n[bold]Core / periphery[/bold] ({len(visibility)} components)")
    for role, colour, meaning in _ROLES:
        if not counts[role]:
            continue
        share = counts[role] / len(visibility)
        members = sorted(name for name, seen in visibility.items() if seen["role"] == role)
        console.print(
            f"  [{colour}]{role:11}[/{colour}] {counts[role]:3} ({share:4.0%})"
            f"  [dim]{meaning}[/dim]"
        )
        console.print(f"    [dim]{', '.join(members[:5])}[/dim]")
    console.print(
        "  [dim]relative to this project's own largest cycle: every system has a core[/dim]"
    )


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


def _emit_index(payload: dict[str, Any], output: Output) -> None:
    console = output.console
    if console is None:
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


def _emit_classes(payload: dict[str, Any], output: Output) -> None:
    console = output.console
    if console is None:
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


def emit_health(payload: dict[str, Any], output: Output) -> None:
    """Risk profiles, worst-first, with the entities that account for each share.

    The share is printed beside the names that carry it rather than alone, because a number
    with nothing attached is the output `docs/metrics.md` section 10.1 refuses.
    """
    console = output.console
    if console is None:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return
    if payload["status"] == "ERROR":
        for path, message in payload["errors"].items():
            console.print(f"[red]{path}[/red]: {message}")
        return

    console.print(
        f"[bold]Health[/bold] [dim]{payload['files']} file(s); informational, never gates[/dim]"
    )
    for rule, found in sorted(payload["profiles"].items(), key=_by_share):
        console.print(
            f"\n  [bold]{rule}[/bold] <= {found['ceiling']:g}   "
            f"[bold]{found['share_over_ceiling']:.1%}[/bold] of "
            f"{found['sloc']:,.0f} lines over budget"
            f"  [dim]({found['entities_over']} of {found['entities']} entities)[/dim]"
        )
        for worst in found["worst"][:5]:
            console.print(
                f"      {worst['value']:>6g}  {worst['entity'][:56]:<56}"
                f" [dim]{worst['path']}:{worst['line']}[/dim]"
            )
    _emit_coupling(console, payload.get("coupling"))


def _emit_coupling(console: Console, found: Any) -> None:
    """Coupling as a distribution, with the bounded rows kept visibly apart.

    No share and no ceiling: CBO and RFC have no calibrated budget, and printing one here
    would assert more than `oxn.calibration` can support. A `>=` in front of a number is the
    whole point of showing it at all -- below L2 an unplaced base or callee makes the value a
    floor, and a reader who cannot see which is which has been given an average of facts and
    guesses.
    """
    if not found or not found["classes"]:
        return
    console.print(
        f"\n  [bold]coupling[/bold] [dim]no ceiling; distribution over "
        f"{found['exact']} exactly-measured of {found['classes']} classes[/dim]"
    )
    for key in ("cbo", "rfc"):
        spread = found[key]
        if spread is None:
            console.print(f"      {key.upper():<4} [dim]no exactly-measured class[/dim]")
            continue
        console.print(
            f"      {key.upper():<4} median {spread['median']:g}"
            f"   p90 {spread['p90']:g}   max {spread['max']:g}"
        )
    if found["bounded"]:
        console.print(
            f"      [dim]{found['bounded']} class(es) left a base or callee unplaced; "
            f"their numbers are lower bounds[/dim]"
        )
    for worst in found["worst"][:5]:
        bound = ">=" if worst["exactness"] == "APPROX" else "  "
        console.print(
            f"      {bound}{worst['cbo']:>3} cbo {worst['rfc']:>3} rfc  "
            f"{worst['entity'][:24]:<24} [dim]{_tail(worst['path'], 26)}[/dim]"
        )


def _tail(path: str, width: int) -> str:
    """The end of a path, marked when it has been cut.

    The end is the informative half of a source path, and a silent trim reads as a real
    path that happens to start oddly.
    """
    return path if len(path) <= width else "…" + path[-(width - 1) :]


def _by_share(item: tuple[str, Any]) -> float:
    return -float(item[1]["share_over_ceiling"])


def _emit_review(payload: dict[str, Any], output: Output) -> None:
    """The measurement, then the comment if one was written.

    The comment is shown *below* the findings it describes, never instead of them: prose is
    the part nobody audits, and putting the numbers first is the same instinct that keeps them
    out of the model's hands in the first place.
    """
    console = output.console
    if console is None:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    files = payload["files"]
    counts = payload["counts"]
    console.print(
        f"[bold]{payload['base']}...{payload['head']}[/bold]  "
        f"{files['changed']} changed, {files['measured']} measured"
        + (f", {files['excluded']} excluded" if files["excluded"] else "")
    )
    console.print(
        f"  [bold]{counts['new']}[/bold] new  "
        f"[bold]{counts['regression']}[/bold] regressed  "
        f"[dim]{counts['baselined']} pre-existing[/dim]"
    )
    for found in payload["findings"][:20]:
        tint = "dim" if found["origin"] == "baselined" else "bold"
        console.print(
            f"  [{tint}]{found['origin']:<11}[/{tint}] {found['message']}"
            f"  [dim]{found['path']}:{found['line']}[/dim]"
        )
    _emit_comment(payload.get("comment"), console)


def _emit_comment(comment: dict[str, Any] | None, console: Console) -> None:
    """The comment that would be posted, and who lost the right to phrase it.

    A refusal is shown *above* the body it fell back to, not instead of it: the body is still
    there, and a reader who sees the prose without the reason would credit the model for
    sentences it did not get to write.
    """
    if comment is None:
        return
    console.print(f"\n[bold]Comment[/bold]  [dim]{comment.get('model', 'oxn')}[/dim]")
    if comment["status"] != "OK":
        invented = ", ".join(comment.get("invented", ()))
        reason = comment.get("note") or (
            f"the writer stated {invented}, which the measurement does not contain"
        )
        console.print(f"  [yellow]prose refused[/yellow] — [dim]{reason}[/dim]")
    for line in comment.get("body", "").splitlines():
        console.print(f"  {line}")
