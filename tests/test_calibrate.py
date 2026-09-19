"""Reading the calibration surface: selection, project overrides, and the two views.

The record itself is asserted in `tests/test_check.py` -- that every gated threshold appears,
that a parameter with observations says what they showed. These are about *getting at* it:
the view that made 276 lines of provenance readable, and the comparison that says whether the
number governing you is still OXN's.
"""

from __future__ import annotations

import pytest

from oxn.calibrate import Selection, UnknownParameter, overrides, select
from oxn.calibration import gate_status, parameters


class _Settings:
    """The two attributes `gate_status` and `overrides` read off a `Config`."""

    def __init__(self, ceilings: dict[str, float], disabled: frozenset[str] = frozenset()):
        self.ceilings = ceilings
        self.disabled = disabled


def _defaults() -> _Settings:
    from pathlib import Path

    from oxn.config import Config

    return Config.defaults(Path.cwd())  # type: ignore[return-value]


def test_no_selection_returns_the_whole_surface() -> None:
    values = parameters()
    assert select(values, Selection(), gate_status(_defaults())) == values


def test_a_name_returns_exactly_that_parameter() -> None:
    chosen = select(parameters(), Selection(name="MAX_NESTING_DEPTH"), {})
    assert [parameter.name for parameter in chosen] == ["MAX_NESTING_DEPTH"]


def test_a_name_is_matched_without_regard_to_case_or_padding() -> None:
    """Typing a threshold name exactly, in caps, is not a thing to demand of a reader."""
    chosen = select(parameters(), Selection(name="  max_nesting_depth "), {})
    assert [parameter.name for parameter in chosen] == ["MAX_NESTING_DEPTH"]


def test_an_unknown_name_raises_with_the_valid_ones() -> None:
    """The list is the useful half: an empty result tells a reader nothing about the typo."""
    with pytest.raises(UnknownParameter) as raised:
        select(parameters(), Selection(name="MAX_NESTING"), {})
    assert "MAX_NESTING_DEPTH" in raised.value.known
    assert raised.value.name == "MAX_NESTING"


def test_a_name_wins_over_a_state_filter() -> None:
    """Asking for one parameter and getting nothing back is the worse answer."""
    chosen = select(
        parameters(), Selection(name="MAX_NESTING_DEPTH", fitted=True), gate_status(_defaults())
    )
    assert [parameter.name for parameter in chosen] == ["MAX_NESTING_DEPTH"]


def test_provisional_and_fitted_partition_the_surface() -> None:
    values, active = parameters(), gate_status(_defaults())
    provisional = select(values, Selection(provisional=True), active)
    fitted = select(values, Selection(fitted=True), active)
    assert len(provisional) + len(fitted) == len(values)
    assert not set(provisional) & set(fitted)


def test_gated_selects_only_parameters_a_rule_can_be_switched_off_for() -> None:
    """`BM25_K1` has no on/off, so it is not a gate however tunable it is."""
    active = gate_status(_defaults())
    gated = select(parameters(), Selection(gated=True), active)
    names = {parameter.name for parameter in gated}
    assert "MAX_COGNITIVE_COMPLEXITY" in names
    assert "BM25_K1" not in names
    assert names <= set(active)


def test_off_selects_what_this_project_switched_off() -> None:
    settings = _Settings(ceilings={}, disabled=frozenset({"parameter_count"}))
    active = gate_status(settings)
    off = select(parameters(), Selection(off=True), active)
    names = {parameter.name for parameter in off}
    assert names == {"MAX_PARAMETERS"}, (
        "only the rule this project switched off should be selected, and `off` is a property "
        f"of `Config.disabled` rather than of the ceilings map: {names}"
    )


def test_a_project_running_the_defaults_has_overridden_nothing() -> None:
    assert overrides(_defaults()) == {}


def test_a_changed_ceiling_is_reported_against_the_default_it_replaced() -> None:
    """The claim nothing made before: the number governing you may not be OXN's.

    Every `provenance` line describes the default. In a project that set its own, those
    sentences are explaining a number that is not in force.
    """
    from oxn import thresholds

    settings = _Settings(ceilings={"cognitive_complexity": 20.0})
    changed = overrides(settings)
    assert changed["MAX_COGNITIVE_COMPLEXITY"] == (
        float(thresholds.MAX_COGNITIVE_COMPLEXITY),
        20.0,
    )


def test_a_ceiling_set_to_its_default_is_not_an_override() -> None:
    """Declaring a value explicitly is not changing it, and reporting it as one is noise."""
    from oxn import thresholds

    same = float(thresholds.MAX_COGNITIVE_COMPLEXITY)
    assert overrides(_Settings(ceilings={"cognitive_complexity": same})) == {}
