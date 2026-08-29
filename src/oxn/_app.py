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
def doctor() -> None:
    """Report the environment OXN found: grammars, toolchains, and indexers."""
    from oxn.doctor import run_doctor

    run_doctor(_console())
