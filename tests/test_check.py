"""`oxn check` — the gate, and the two properties that make it trustworthy.

Everything else OXN exposes reports; this decides. So the tests here are less about metric
values (those live in `test_metrics_*.py`) than about *judgement*: what may block, what a
finding's identity is, and what a baseline forgives.
"""

from __future__ import annotations

import json

import pytest

from oxn.check import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_VIOLATIONS,
    CheckReport,
    Finding,
    run_check,
    write_baseline,
)
from oxn.config import Config, ConfigError
from oxn.graph.model import Resolution
from oxn.metrics.engine import MetricValue

TANGLED = (
    "def tangled(rows):\n"
    "    for row in rows:\n"
    "        if row:\n"
    "            for cell in row:\n"
    "                if cell:\n"
    "                    if cell > 1:\n"
    "                        return cell\n"
    "    return None\n"
)
CALM = "def one(a):\n    return a\n"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---- what may block -------------------------------------------------------------------


def test_an_exact_measurement_over_the_ceiling_blocks(project) -> None:
    (project / "tangled.py").write_text(TANGLED)
    report = run_check(["tangled.py"])
    assert report.exit_code == EXIT_VIOLATIONS
    assert any(finding.rule == "cognitive_complexity" for finding in report.blocking)


def test_clean_code_passes_with_exit_zero(project) -> None:
    (project / "calm.py").write_text(CALM)
    report = run_check(["calm.py"])
    assert report.passed
    assert report.exit_code == EXIT_OK


def test_a_missing_path_is_an_error_not_a_violation(project) -> None:
    """Exit 1 says OXN could not answer. Exit 2 would claim the code is wrong."""
    report = run_check(["nope.py"])
    assert report.exit_code == EXIT_ERROR
    assert not report.blocking


def test_an_upper_bound_is_reported_but_never_blocks() -> None:
    """ADR-0002's gate policy, which is the reason `bound` is persisted at all.

    An approximate value may block a ceiling only when it is a *lower* bound -- a lower
    bound already over the line proves the true value is too. An upper bound proves nothing
    in that direction, so it is advisory however large it looks.
    """
    upper = MetricValue(
        key="cognitive_complexity",
        value=99.0,
        exactness="APPROX",
        resolution=Resolution.L1,
        bound="upper",
    )
    assert not upper.can_block_ceiling

    lower = MetricValue(
        key="cognitive_complexity",
        value=99.0,
        exactness="APPROX",
        resolution=Resolution.L1,
        bound="lower",
    )
    assert lower.can_block_ceiling


def test_an_advisory_finding_does_not_fail_the_run() -> None:
    report = CheckReport(findings=[_finding(blocking=False)])
    assert report.passed
    assert report.exit_code == EXIT_OK
    assert report.advisory and not report.blocking


# ---- a ceiling is about a kind of thing -----------------------------------------------


def test_the_function_length_ceiling_is_not_applied_to_modules(project) -> None:
    """60 lines is a statement about a function.

    Applied to a module it flags every file in the project against a limit that was never
    about files -- which is exactly what the first wiring of this command did.
    """
    (project / "long.py").write_text(
        "".join(f"def f{n}(a):\n    return a\n\n\n" for n in range(40))
    )
    report = run_check(["long.py"])
    flagged = {(finding.rule, finding.entity) for finding in report.findings}
    assert not any(rule == "function_sloc" for rule, _ in flagged)
    assert not any(entity.endswith("long") and rule == "function_sloc" for rule, entity in flagged)


def test_the_file_length_ceiling_still_applies_to_the_module(project) -> None:
    (project / "huge.py").write_text("".join(f"x{n} = {n}\n" for n in range(600)))
    report = run_check(["huge.py"])
    assert any(finding.rule == "file_sloc" for finding in report.findings)


# ---- finding identity -----------------------------------------------------------------


def test_a_findings_identity_ignores_its_line_number() -> None:
    """Otherwise inserting a line above a function re-reports it as brand new."""
    before = _finding(line=10)
    after = _finding(line=94)
    assert before.key == after.key


def test_identity_separates_rules_paths_and_entities() -> None:
    base = _finding()
    assert base.key != _finding(rule="cyclomatic_complexity").key
    assert base.key != _finding(path="other.py").key
    assert base.key != _finding(entity="mod.other").key


# ---- the baseline, which is a ratchet -------------------------------------------------


def test_a_baselined_violation_stops_failing_the_build(project) -> None:
    (project / "tangled.py").write_text(TANGLED)
    settings = Config.load(project)
    first = run_check(["tangled.py"], config=settings, use_baseline=False)
    assert write_baseline(first, settings.baseline_path) > 0

    again = run_check(["tangled.py"], config=settings)
    assert again.passed, "recorded debt must not fail the build"
    assert again.baselined and not again.blocking


def test_a_baselined_violation_getting_worse_fails_again(project) -> None:
    """The half that makes a baseline a ratchet rather than an amnesty."""
    (project / "tangled.py").write_text(TANGLED)
    settings = Config.load(project)
    write_baseline(
        run_check(["tangled.py"], config=settings, use_baseline=False), settings.baseline_path
    )

    (project / "tangled.py").write_text(
        TANGLED.replace(
            "                        return cell\n",
            "                        for extra in cell:\n"
            "                            if extra:\n"
            "                                return extra\n",
        )
    )
    worse = run_check(["tangled.py"], config=settings)
    assert not worse.passed
    assert worse.regressed, "a recorded violation may stay, but it may not grow"
    assert worse.exit_code == EXIT_VIOLATIONS


def test_a_new_violation_fails_even_with_a_baseline(project) -> None:
    (project / "tangled.py").write_text(TANGLED)
    settings = Config.load(project)
    write_baseline(
        run_check(["tangled.py"], config=settings, use_baseline=False), settings.baseline_path
    )

    (project / "fresh.py").write_text(TANGLED.replace("tangled", "fresh"))
    report = run_check(["tangled.py", "fresh.py"], config=settings)
    assert not report.passed
    assert {finding.path for finding in report.blocking} == {"fresh.py"}


def test_the_baseline_file_says_what_it_is(project) -> None:
    """Someone will find this file in a diff and need to know whether to trust it."""
    (project / "tangled.py").write_text(TANGLED)
    settings = Config.load(project)
    write_baseline(
        run_check(["tangled.py"], config=settings, use_baseline=False), settings.baseline_path
    )

    recorded = json.loads(settings.baseline_path.read_text())
    assert "note" in recorded and "worse" in recorded["note"]
    assert recorded["scope"] == "files"
    assert recorded["violations"]


def test_a_corrupt_baseline_is_ignored_rather_than_fatal(project) -> None:
    """A gate that crashes on a bad state file is a gate that gets switched off."""
    (project / "tangled.py").write_text(TANGLED)
    settings = Config.load(project)
    settings.baseline_path.parent.mkdir(parents=True, exist_ok=True)
    settings.baseline_path.write_text("{not json")
    report = run_check(["tangled.py"], config=settings)
    assert report.blocking, "with no usable baseline, everything is a violation again"


# ---- configuration --------------------------------------------------------------------


def test_an_unconfigured_project_still_checks_something(project) -> None:
    settings = Config.load(project)
    assert settings.ceilings["cognitive_complexity"] == 12
    assert settings.layers == ()


def test_a_ceiling_override_is_honoured(project) -> None:
    (project / "oxn.yaml").write_text("ceilings:\n  cognitive_complexity: 100\n")
    (project / "tangled.py").write_text(TANGLED)
    report = run_check(["tangled.py"], config=Config.load(project))
    assert not any(finding.rule == "cognitive_complexity" for finding in report.findings)


def test_a_layer_may_set_its_own_ceiling(project) -> None:
    (project / "oxn.yaml").write_text(
        "layers:\n  generated: ['gen/*']\nlayer_ceilings:\n  generated:\n"
        "    cognitive_complexity: 100\n"
    )
    (project / "gen").mkdir()
    (project / "gen" / "tangled.py").write_text(TANGLED)
    report = run_check(["gen"], config=Config.load(project))
    assert not any(finding.rule == "cognitive_complexity" for finding in report.findings)


def test_a_misspelt_rule_is_rejected_not_ignored(project) -> None:
    """Silently ignoring an unknown key means a ceiling nobody is enforcing."""
    (project / "oxn.yaml").write_text("ceilings:\n  cognitive_complexty: 20\n")
    with pytest.raises(ConfigError, match="not a gated rule"):
        Config.load(project)


def test_a_layer_ceiling_for_an_undeclared_layer_is_rejected(project) -> None:
    (project / "oxn.yaml").write_text("layer_ceilings:\n  nowhere:\n    cognitive_complexity: 20\n")
    with pytest.raises(ConfigError, match="undeclared layer"):
        Config.load(project)


def test_advisory_paths_are_not_gated(project) -> None:
    (project / "oxn.yaml").write_text("advisory: ['vendor/*']\n")
    (project / "vendor").mkdir()
    (project / "vendor" / "tangled.py").write_text(TANGLED)
    report = run_check(["vendor"], config=Config.load(project))
    assert report.passed


def _finding(**overrides) -> Finding:
    fields = {
        "rule": "cognitive_complexity",
        "path": "mod.py",
        "entity": "mod.f",
        "line": 10,
        "value": 20.0,
        "ceiling": 12.0,
    }
    return Finding(**{**fields, **overrides})


# ---- the calibration surface ----------------------------------------------------------


def test_every_gated_threshold_states_its_provenance() -> None:
    """A number with no stated provenance is folklore, and folklore gets a gate switched off.

    This fails when someone adds a threshold to `thresholds.py` without recording where it
    came from -- which is the moment the omission is cheap to fix.
    """
    from oxn import thresholds
    from oxn.calibration import parameters
    from oxn.config import GATED_METRICS

    documented = {parameter.name for parameter in parameters()}
    gated = {gate.threshold for gate in GATED_METRICS.values()}
    assert gated <= documented, f"gated but undocumented: {sorted(gated - documented)}"
    for name in documented:
        if hasattr(thresholds, name):
            recorded = next(p for p in parameters() if p.name == name)
            assert recorded.value == float(getattr(thresholds, name)), (
                f"{name} is documented as {recorded.value} but is actually "
                f"{getattr(thresholds, name)}"
            )


def test_a_measured_parameter_says_how_many_observations_back_it() -> None:
    """`TRIVIAL_HELPER` is fitted to two examples, and that must be visible, not implied."""
    from oxn.calibration import Evidence, parameters

    for parameter in parameters():
        if parameter.evidence is Evidence.MEASURED:
            assert parameter.observations > 0
            assert parameter.fit_when, "a measured parameter should say what would improve it"
        else:
            assert parameter.observations == 0
