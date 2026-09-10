"""Turning a gated rule off, which is a decision and has to look like one.

Every gated rule is a constraint spent from a budget nobody has measured the size of:
`MAX_BUNDLE_CONSTRAINTS = 7` is a judgement, and ADR-0003's own note says the decay knee is
unknown. So the question "should this rule be on here?" is one a project must be able to
answer, and answer visibly -- not by editing OXN, and not by raising a ceiling to a number
so large it cannot fire, which reads as a threshold rather than as a decision.

`off` is the absence of a ceiling row, not an infinite one. Every ceiling rule joins on that
row, so absence already means "does not apply"; a second spelling would give the engine two
ways to say the same thing and a reader two things to check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oxn.check import run_check
from oxn.config import Config, ConfigError

DEEP = (
    "def walk(rows):\n"
    "    for row in rows:\n"
    "        if row:\n"
    "            for cell in row:\n"
    "                if cell:\n"
    "                    for part in cell:\n"
    "                        if part:\n"
    "                            return part\n"
    "    return None\n"
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "deep.py").write_text(DEEP)
    return tmp_path


def _rules(project: Path, config: str) -> set[str]:
    (project / "oxn.yaml").write_text(config)
    return {finding.rule for finding in run_check(["deep.py"], use_baseline=False).blocking}


def test_a_rule_switched_off_stops_firing_and_the_others_do_not(project: Path) -> None:
    """The whole feature, and the control beside it: only the named rule goes quiet."""
    assert _rules(project, "version: 1\n") >= {"cognitive_complexity", "max_nesting_depth"}

    remaining = _rules(project, "version: 1\nceilings:\n  max_nesting_depth: off\n")
    assert "max_nesting_depth" not in remaining
    assert "cognitive_complexity" in remaining, "one rule off is not the gate off"


def test_off_may_be_written_as_yaml_reads_it_or_as_it_is_typed(project: Path) -> None:
    """Bare `off` is the boolean False in YAML and `"off"` is the string.

    A config that behaved differently depending on which one was typed would be a trap: the
    two look identical in a diff and the quoted form is what an editor's formatter produces.
    """
    for spelling in ("off", '"off"', "false", '"disabled"'):
        found = _rules(project, f"version: 1\nceilings:\n  max_nesting_depth: {spelling}\n")
        assert "max_nesting_depth" not in found, spelling


def test_switching_off_the_rule_shredding_protects_switches_shredding_off(project: Path) -> None:
    """`shredding` has no ceiling of its own -- it follows `cognitive_complexity`.

    It exists to stop that one ceiling being met by splitting instead of simplifying, so with
    the ceiling gone there is nothing to protect, and leaving it on would keep rejecting the
    split version of a function whose honest version now passes. It also cannot be left on
    mechanically: `ceiling_for` reads the followed rule's limit, which is no longer there.
    """
    settings = Config._from_mapping(project, {"ceilings": {"cognitive_complexity": "off"}})
    assert settings.disabled == {"cognitive_complexity", "shredding"}
    assert "shredding" not in settings.ceilings


def test_a_value_that_is_neither_a_number_nor_off_is_refused(project: Path) -> None:
    """Silently ignoring it would leave the rule on while the file says otherwise."""
    (project / "oxn.yaml").write_text("version: 1\nceilings:\n  parameter_count: maybe\n")
    with pytest.raises(ConfigError) as raised:
        Config.load(project)
    assert "number or `off`" in str(raised.value)


def test_calibration_says_which_rules_this_project_actually_applies(project: Path) -> None:
    """A ceiling nobody applies is a default, not a threshold, and the surface said neither.

    `oxn calibration` is the answer to "where did this number come from"; it listed
    `MAX_NESTING_DEPTH = 4` with its provenance whether or not the rule was switched off.
    """
    from oxn.calibration import gate_status

    settings = Config._from_mapping(project, {"ceilings": {"max_nesting_depth": "off"}})
    status = gate_status(settings)

    assert status["MAX_NESTING_DEPTH"] is False
    assert status["MAX_COGNITIVE_COMPLEXITY"] is True
