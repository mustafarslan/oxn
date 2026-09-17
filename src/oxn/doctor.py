"""``oxn doctor`` -- report what OXN found in this environment.

Two things are worth reporting before any analysis exists:

* which **grammars** load (the runtime dependency that must work), and
* which **SCIP indexers and toolchains** are present, since ADR-0002 makes the L2
  resolution rung opt-in and *detected*, never installed on the user's behalf.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from oxn.languages import LAUNCH_LANGUAGES

if TYPE_CHECKING:  # pragma: no cover
    from rich.console import Console


#: The indexer registry lives in `oxn.scip.runner` and is imported, never restated.
#:
#: `doctor` kept its own copy until 2026-09-06, and a second list is a list that drifts: it
#: advertised `scip-go`, `scip-java` and `rust-analyzer` under "install this to enable exact
#: coupling metrics" while `oxn index` had entries for none of them, so following the advice
#: bought nothing. The Go hint had also gone stale -- the project changed organisation, and
#: `go install github.com/sourcegraph/scip-go/...` fails on the module path inside. A report
#: about the environment is worth exactly as much as its agreement with the code that acts
#: on it, so there is now one list and this file reads it.


def check_grammars() -> dict[str, str | None]:
    """Load every launch grammar. Maps language name to an error string, or ``None`` if fine."""
    results: dict[str, str | None] = {}
    for name in LAUNCH_LANGUAGES:
        try:
            from oxn.languages import get_language

            get_language(name)
            results[name] = None
        except Exception as exc:  # noqa: BLE001 -- doctor reports failures, never raises
            results[name] = f"{type(exc).__name__}: {exc}"
    return results


def check_indexers() -> dict[str, str | None]:
    """Map each language to its indexer's resolved path, or ``None`` if not installed."""
    from oxn.scip.runner import available_indexers

    return available_indexers()


def run_doctor(console: Console) -> None:
    """Print the environment report."""
    from oxn import __version__

    console.print(f"[bold]oxn {__version__}[/bold]\n")

    console.print("[bold]Grammars[/bold] (tree-sitter-language-pack)")
    for name, error in check_grammars().items():
        if error is None:
            console.print(f"  [green]ok[/green]    {name}")
        else:
            console.print(f"  [red]FAIL[/red]  {name}: {error}")

    console.print("\n[bold]SCIP indexers[/bold] (optional; enables exact coupling metrics)")
    from oxn.scip.runner import INDEXERS, UNWIRED

    found = check_indexers()
    for language, indexer in INDEXERS.items():
        path = found[language]
        if path:
            console.print(f"  [green]ok[/green]    {language:11s} {indexer.command} -> {path}")
            # A user asking what they have should learn what it costs here, before the run
            # rather than during it.
            if indexer.caveat:
                console.print(f"        [dim]{indexer.caveat}; up to {indexer.timeout}s[/dim]")
        else:
            console.print(
                f"  [dim]--[/dim]    {language:11s} {indexer.command} not found "
                f"([dim]install: {indexer.install_hint}[/dim])"
            )
    # A launch language with no indexer at all is a different answer from one whose indexer
    # is merely uninstalled, and the difference matters: no command will fix the second.
    for language, why in UNWIRED.items():
        console.print(f"  [yellow]n/a[/yellow]   {language:11s} no L2 ([dim]{why}[/dim])")
    console.print(
        "\n[dim]Missing indexers are not an error. Without them OXN resolves names at "
        "L0/L1 and marks the affected metrics APPROX (ADR-0002).[/dim]"
    )
