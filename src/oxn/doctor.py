"""``oxn doctor`` -- report what OXN found in this environment.

Two things are worth reporting before any analysis exists:

* which **grammars** load (the runtime dependency that must work), and
* which **SCIP indexers and toolchains** are present, since ADR-0002 makes the L2
  resolution rung opt-in and *detected*, never installed on the user's behalf.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from oxn.languages import LAUNCH_LANGUAGES

if TYPE_CHECKING:  # pragma: no cover
    from rich.console import Console


@dataclass(frozen=True)
class Indexer:
    """A SCIP indexer OXN can use for the L2 resolution rung."""

    language: str
    command: str
    install_hint: str


#: All Apache-2.0, free, offline. OXN detects and instructs; it never installs these.
SCIP_INDEXERS: tuple[Indexer, ...] = (
    Indexer("python", "scip-python", "npm install -g @sourcegraph/scip-python"),
    Indexer("typescript", "scip-typescript", "npm install -g @sourcegraph/scip-typescript"),
    Indexer("go", "scip-go", "go install github.com/sourcegraph/scip-go/cmd/scip-go@latest"),
    Indexer("java", "scip-java", "see https://sourcegraph.github.io/scip-java/"),
    Indexer("rust", "rust-analyzer", "rustup component add rust-analyzer"),
)


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
    """Map each indexer command to its resolved path, or ``None`` if not installed."""
    return {ix.command: shutil.which(ix.command) for ix in SCIP_INDEXERS}


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
    found = check_indexers()
    for ix in SCIP_INDEXERS:
        path = found[ix.command]
        if path:
            console.print(f"  [green]ok[/green]    {ix.command} -> {path}")
        else:
            console.print(
                f"  [dim]--[/dim]    {ix.command} not found "
                f"([dim]{ix.language}[/dim]; install: {ix.install_hint})"
            )
    console.print(
        "\n[dim]Missing indexers are not an error. Without them OXN resolves names at "
        "L0/L1 and marks the affected metrics APPROX (ADR-0002).[/dim]"
    )
