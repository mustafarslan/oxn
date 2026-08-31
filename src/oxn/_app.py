"""The human-facing typer application.

Imported lazily by :func:`oxn.cli.main`, never by the hook fast path. Anything that is
pleasant for a person and expensive for a machine belongs here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from oxn import __version__

app = typer.Typer(
    name="oxn",
    help="Local architecture and quality gatekeeper for LLM coding agents.",
    no_args_is_help=True,
    add_completion=False,
)


if TYPE_CHECKING:  # pragma: no cover
    from rich.console import Console


def _console() -> Console:
    from rich.console import Console

    return Console()


@app.command()
def version() -> None:
    """Print the OXN version."""
    _console().print(f"oxn {__version__}")


@app.command()
def check(
    paths: list[str] = typer.Argument(None, help="Files or directories to check."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
    deep: bool = typer.Option(
        False, "--deep", help="Also check repository-scoped contracts. Too slow for a hook."
    ),
    no_baseline: bool = typer.Option(
        False, "--no-baseline", help="Ignore .oxn/baseline.json and report every violation."
    ),
) -> None:
    """Check files against the project's architectural and quality invariants.

    The ``--json`` form is dispatched by :mod:`oxn.cli` before typer is ever imported, so
    this function is only reached on the human path.
    """
    from oxn.check import render, run_check
    from oxn.config import ConfigError

    console = _console()
    try:
        report = run_check(paths or ["."], deep=deep, use_baseline=not no_baseline)
    except ConfigError as error:
        console.print(f"[red]oxn.yaml[/red]: {error}")
        raise typer.Exit(1) from error

    if json_output:
        import json

        console.print_json(json.dumps(report.as_dict()))
    else:
        render(report, console)
    raise typer.Exit(report.exit_code)


@app.command()
def baseline(
    paths: list[str] = typer.Argument(None, help="Files or directories to record."),
    deep: bool = typer.Option(False, "--deep", help="Include repository-scoped contracts."),
) -> None:
    """Record today's violations as accepted debt, so only *new* ones fail.

    The ratchet, not an amnesty: a recorded violation stops failing the build, but the same
    violation getting worse starts failing it again, and anything new fails immediately.
    This is what makes `oxn check` adoptable on a repository that predates it.
    """
    from oxn.check import run_check, write_baseline
    from oxn.config import Config, ConfigError

    console = _console()
    try:
        settings = Config.load()
        report = run_check(paths or ["."], config=settings, deep=deep, use_baseline=False)
    except ConfigError as error:
        console.print(f"[red]oxn.yaml[/red]: {error}")
        raise typer.Exit(1) from error

    if report.errors:
        for path, problem in report.errors.items():
            console.print(f"[red]error[/red] {path}: {problem}")
        raise typer.Exit(1)

    recorded = write_baseline(report, settings.baseline_path)
    where = settings.baseline_path.relative_to(settings.root)
    console.print(f"recorded [bold]{recorded}[/bold] violation(s) as accepted debt in {where}")
    console.print("[dim]new violations, and these getting worse, will still fail[/dim]")


@app.command()
def init(
    no_hook: bool = typer.Option(
        False, "--no-hook", help="Write config and docs but do not install the hook."
    ),
) -> None:
    """Wire OXN into this repository. Additive and idempotent -- run it twice safely.

    Nothing is overwritten. `oxn.yaml` is written only if absent, the `CLAUDE.md` section
    lives between markers, and an existing `.claude/settings.json` is merged rather than
    replaced.
    """
    from oxn.init import run_init

    console = _console()
    report = run_init(with_hook=not no_hook)
    for label, paths in (
        ("created", report.created),
        ("updated", report.updated),
        ("unchanged", report.unchanged),
    ):
        for path in paths:
            colour = {"created": "green", "updated": "cyan"}.get(label, "dim")
            console.print(f"[{colour}]{label:9}[/{colour}] {path}")
    for note in report.notes:
        console.print(f"[yellow]note[/yellow]      {note}")
    console.print(
        "\n[dim]next: `oxn check` to see where you stand, `oxn baseline` to accept it[/dim]"
    )


@app.command()
def calibration(
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List every tunable threshold with the evidence behind it.

    A number with no stated provenance is folklore, and folklore is what makes developers
    distrust a gate. This is also the surface P10's calibration work optimises over.
    """
    from oxn.calibration import parameters as _parameters
    from oxn.calibration import summary

    console = _console()
    report = summary()
    if json_output:
        import json

        console.print_json(json.dumps(report))
        return

    for parameter in _parameters():
        marker = (
            "[yellow]provisional[/yellow]" if parameter.is_provisional else "[green]fitted[/green]"
        )
        console.print(
            f"[bold]{parameter.name}[/bold] = {parameter.value:g}  "
            f"{marker} {parameter.evidence.value} n={parameter.observations}"
        )
        console.print(f"  [dim]{parameter.provenance}[/dim]")
        if parameter.fit_when:
            console.print(f"  [dim]fit when: {parameter.fit_when}[/dim]")
    console.print(f"\n[dim]{report['provisional']}/{report['total']} provisional[/dim]")
    console.print(f"[dim]{report['note']}[/dim]")


@app.command()
def parse(
    paths: list[str] = typer.Argument(None, help="Files or directories to parse."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
    force: bool = typer.Option(False, "--force", help="Ignore the cache and re-parse."),
    stats: bool = typer.Option(False, "--stats", help="Show cache totals instead of entities."),
) -> None:
    """Parse sources into the code graph and report what was built.

    A debugging surface for the parse layer: it shows the containment skeleton the metric
    engine will read, and whether a run hit the cache.
    """
    from oxn.render import Output
    from oxn.report import run_parse

    run_parse(
        paths or ["."],
        Output(console=None if json_output else _console()),
        force=force,
        stats_only=stats,
    )


@app.command()
def metrics(
    paths: list[str] = typer.Argument(None, help="Files or directories to measure."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
    sort_by: str = typer.Option("cognitive_complexity", "--sort-by", help="Metric to rank by."),
    limit: int = typer.Option(20, "--limit", help="How many entities to show."),
    explain: bool = typer.Option(
        False, "--explain", help="Show the increment trail for the worst offender."
    ),
) -> None:
    """Rank code by a Tier-1 metric.

    Every point of a cognitive-complexity score can be traced to a line and a reason:
    ``oxn metrics --explain`` prints that trail.
    """
    from oxn.render import Output
    from oxn.report import run_metrics

    run_metrics(
        paths or ["."],
        Output(console=None if json_output else _console()),
        sort_by=sort_by,
        limit=limit,
        explain=explain,
    )


@app.command()
def volume(
    paths: list[str] = typer.Argument(None, help="Files or directories to analyse."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
    no_history: bool = typer.Option(False, "--no-history", help="Skip git history."),
) -> None:
    """Report duplication, verbosity, structural erosion and hotspots.

    These are the signals agent-written code degrades: volume correlates with
    architectural decay where prompt specificity does not.
    """
    from oxn.render import Output
    from oxn.report import run_volume

    run_volume(
        paths or ["."],
        Output(console=None if json_output else _console()),
        include_history=not no_history,
    )


@app.command()
def arch(
    paths: list[str] = typer.Argument(None, help="Files or directories to analyse."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
    granularity: str = typer.Option(
        "directory", "--by", help="Component granularity: directory | file | top."
    ),
    show_unresolved: bool = typer.Option(
        False, "--unresolved", help="List imports that could not be resolved."
    ),
) -> None:
    """Report the dependency graph: cycles, Martin metrics, Lakos levels, smells.

    Report-path only. Building the graph compares every file against every other, so it
    never runs on the hook's per-edit budget.
    """
    from oxn.render import Output
    from oxn.report import run_arch

    run_arch(
        paths or ["."],
        Output(console=None if json_output else _console()),
        granularity=granularity,
        show_unresolved=show_unresolved,
    )


@app.command()
def index(
    paths: list[str] = typer.Argument(None, help="Directory to index."),
    language: str = typer.Option("python", "--language", help="Language to index."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
    reuse: str = typer.Option(
        "", "--index-file", help="Ingest an existing .scip file instead of generating one."
    ),
) -> None:
    """Build a SCIP index and merge compiler-grade symbols into the graph.

    This is resolution rung L2 (ADR-0002): it makes coupling and call-graph metrics exact
    rather than heuristic. It needs a per-language indexer, which OXN detects but never
    installs -- run `oxn doctor` to see what is available.
    """
    from oxn.render import Output
    from oxn.report import run_index

    run_index(
        paths or ["."],
        Output(console=None if json_output else _console()),
        language=language,
        index_file=reuse or None,
    )


@app.command()
def classes(
    paths: list[str] = typer.Argument(None, help="Files or directories to analyse."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
    sort_by: str = typer.Option(
        "lcom_star", "--sort-by", help="lcom_star | lcom4 | wmc | cbo | rfc"
    ),
    limit: int = typer.Option(20, "--limit", help="How many classes to show."),
) -> None:
    """Report class cohesion (LCOM) and coupling (the CK suite).

    These are the metrics that detect the "Modular Mirage" -- file-level modularity with no
    semantic cohesion -- which is the documented signature of agent-written code.
    """
    from oxn.render import Output
    from oxn.report import run_classes

    run_classes(
        paths or ["."],
        Output(console=None if json_output else _console()),
        sort_by=sort_by,
        limit=limit,
    )


@app.command()
def doctor() -> None:
    """Report the environment OXN found: grammars, toolchains, and indexers."""
    from oxn.doctor import run_doctor

    run_doctor(_console())
