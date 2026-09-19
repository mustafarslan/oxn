"""Reading the calibration surface: which parameters you asked for, and how to show them.

`calibration.py` is the record -- fifteen parameters, each with its value, its evidence and
several hundred words of provenance. This module is how a person gets at it, and the split is
not tidiness: that file sits at 506 SLOC against a ceiling of 500, and OXN rejected the first
version of this code for putting the view in with the data. The gate was right. What a
threshold *is* and how it is *presented* change for unrelated reasons.

The presentation problem is real. Fifteen parameters with provenance running to 2,230
characters is 276 lines of terminal output, and a wall nobody reads is evidence nobody checks
-- which defeats the point of recording provenance at all. So the default is one line each,
and the prose is a request: `--verbose`, or naming a single parameter.

Nothing here decides what is true. It selects, compares against this project's configuration,
and prints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from oxn.calibration import Parameter, gate_status, parameters, summary

if TYPE_CHECKING:  # pragma: no cover
    from rich.console import Console

    from oxn.render import Output


@dataclass(frozen=True, slots=True)
class Selection:
    """Which parameters a caller asked for, and how much of each to show.

    One object rather than six arguments because they travel together and because the
    parameter-count ceiling is five -- a function taking all of them plus an output has
    already lost.
    """

    name: str = ""
    provisional: bool = False
    fitted: bool = False
    gated: bool = False
    off: bool = False
    verbose: bool = False


class UnknownParameter(LookupError):
    """A name that is not on the surface, carrying the valid ones.

    Someone who typed `MAX_NESTING` wants the list far more than it wants an empty result,
    and an empty result is what a filter would silently have given them.
    """

    def __init__(self, name: str, known: list[str]) -> None:
        super().__init__(f"no parameter named {name!r}")
        self.name = name
        self.known = known


#: What this project has done to a parameter that OXN ships a default for: whether the rule
#: is switched on, and whether the number is still OXN's.
State = tuple[dict[str, bool], dict[str, tuple[float, float]]]


def overrides(settings: object) -> dict[str, tuple[float, float]]:
    """Threshold constant -> (what OXN ships, what this project set), where they differ.

    `gate_status` answers whether a rule is *on*. This answers whether its number is still
    OXN's, which nothing said before: a project running `cognitive_complexity: 20` is not
    running OXN's gate, and every provenance line was describing the default it replaced.
    """
    from oxn import thresholds
    from oxn.config import GATED_METRICS

    ceilings: dict[str, float] = getattr(settings, "ceilings", {})
    changed: dict[str, tuple[float, float]] = {}
    for rule, gate in GATED_METRICS.items():
        default = float(getattr(thresholds, gate.threshold))
        if rule in ceilings and ceilings[rule] != default:
            changed[gate.threshold] = (default, ceilings[rule])
    return changed


def _wanted(parameter: Parameter, selection: Selection, on: bool | None) -> bool:
    """Does one parameter survive the state filters? `on` is None when the rule has no switch."""
    if selection.provisional and not parameter.is_provisional:
        return False
    if selection.fitted and parameter.is_provisional:
        return False
    if selection.gated and on is None:
        return False
    return not (selection.off and on is not False)


def select(
    values: list[Parameter], selection: Selection, active: dict[str, bool]
) -> list[Parameter]:
    """The parameters a selection names, in declaration order.

    A name wins over the state filters. Asking for one parameter and being handed nothing
    because it happens to be fitted is a worse answer than the one that was asked for.
    """
    if selection.name:
        wanted = selection.name.strip().upper()
        found = [parameter for parameter in values if parameter.name == wanted]
        if not found:
            raise UnknownParameter(selection.name, [parameter.name for parameter in values])
        return found
    return [
        parameter
        for parameter in values
        if _wanted(parameter, selection, active.get(parameter.name))
    ]


def _marks(parameter: Parameter, state: State) -> str:
    """The two things true of *this project* rather than of OXN."""
    active, diffs = state
    notes = []
    if active.get(parameter.name) is False:
        notes.append("[red]off here[/red]")
    if parameter.name in diffs:
        notes.append(f"[cyan]set to {diffs[parameter.name][1]:g}[/cyan]")
    return ("  " + " ".join(notes)) if notes else ""


def _headline(parameter: Parameter, state: State, width: int = 0) -> str:
    """The one line every view starts with, padded for a table or not for a detail."""
    marker = "provisional" if parameter.is_provisional else "fitted"
    colour = "yellow" if parameter.is_provisional else "green"
    name = f"{parameter.name:<{width}}" if width else parameter.name
    # Right-align the numbers in the table so they read as a column; in the detail view the
    # value belongs against the `=` instead.
    value = f"{parameter.value:>6g}" if width else f"{parameter.value:g}"
    joiner = "  " if width else " = "
    return (
        f"[bold]{name}[/bold]{joiner}{value}  "
        f"[{colour}]{marker}[/{colour}] {parameter.evidence.value} "
        f"n={parameter.observations}{_marks(parameter, state)}"
    )


def render_table(values: list[Parameter], console: Console, state: State) -> None:
    """One line per parameter: the scannable view, and the default."""
    width = max((len(parameter.name) for parameter in values), default=0)
    for parameter in values:
        console.print(_headline(parameter, state, width))


def render_detail(values: list[Parameter], console: Console, state: State) -> None:
    """Value, provenance, and what would fit it -- the view read before trusting a number."""
    for parameter in values:
        console.print(_headline(parameter, state))
        console.print(f"  [dim]{parameter.provenance}[/dim]")
        if parameter.fit_when:
            console.print(f"  [dim]fit when: {parameter.fit_when}[/dim]")


def run_calibration(output: Output, selection: Selection, settings: object) -> None:
    """Report the parameter surface, filtered and at the requested depth."""
    from oxn.render import _emit

    active = gate_status(settings)
    state: State = (active, overrides(settings))
    chosen = select(parameters(), selection, active)

    if output.as_json:
        payload = summary()
        payload["parameters"] = [parameter.as_dict() for parameter in chosen]
        payload["disabled"] = sorted(name for name, on in active.items() if not on)
        payload["overridden"] = {
            name: {"default": default, "project": project}
            for name, (default, project) in state[1].items()
        }
        _emit(payload, output)
        return

    console = output.console
    assert console is not None
    if selection.name or selection.verbose:
        render_detail(chosen, console, state)
    else:
        render_table(chosen, console, state)
    console.print(f"\n[dim]{len(chosen)} of {len(parameters())} shown.[/dim]")
