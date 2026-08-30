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
) -> None:
    """Check files against the project's architectural and quality invariants.

    The ``--json`` form is dispatched by :mod:`oxn.cli` before typer is ever imported.
    """
    console = _console()
    console.print("[yellow]The analysis engine is not built yet[/yellow] (roadmap phases P1-P2).")
    console.print(f"Would check: {paths or ['.']}")


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
    from oxn.report import run_parse

    run_parse(
        paths or ["."],
        json_output=json_output,
        force=force,
        stats_only=stats,
        console=None if json_output else _console(),
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
    from oxn.report import run_metrics

    run_metrics(
        paths or ["."],
        sort_by=sort_by,
        limit=limit,
        explain=explain,
        json_output=json_output,
        console=None if json_output else _console(),
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
    from oxn.report import run_volume

    run_volume(
        paths or ["."],
        json_output=json_output,
        include_history=not no_history,
        console=None if json_output else _console(),
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
    from oxn.report import run_arch

    run_arch(
        paths or ["."],
        granularity=granularity,
        show_unresolved=show_unresolved,
        json_output=json_output,
        console=None if json_output else _console(),
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
    from oxn.report import run_index

    run_index(
        paths or ["."],
        language=language,
        index_file=reuse or None,
        json_output=json_output,
        console=None if json_output else _console(),
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
    from oxn.report import run_classes

    run_classes(
        paths or ["."],
        sort_by=sort_by,
        limit=limit,
        json_output=json_output,
        console=None if json_output else _console(),
    )


@app.command()
def doctor() -> None:
    """Report the environment OXN found: grammars, toolchains, and indexers."""
    from oxn.doctor import run_doctor

    run_doctor(_console())
