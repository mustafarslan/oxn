"""Complexity spread across the methods of one class, and what the gate sees.

`docs/metrics.md` section 10.5 pairs every per-function ceiling with a class-level aggregate
"so shredding is also penalised". Two of that section's four mitigations did not survive
measurement -- complexity mass on 2026-08-30, the risk profile on 2026-09-09
(`tests/test_risk_profile.py`). This one does, and these are the controls that decide it.

Six variants of one router class, each the same routing work:

===========  ==================================================  ===================
variant      what it is                                          `oxn check`
===========  ==================================================  ===================
before       one honest method, cognitive 28                     blocked, correctly
spread       split by HTTP verb -- a *good* refactoring          passes, correctly
gamed        14 private trivial helpers                          blocked by `shredding`
plump        the same, each helper lifted above TRIVIAL_HELPER   blocked by `shredding`
public       the same shred with public names                    **passes**
shared       the same shred, every helper given a second caller  **passes**
===========  ==================================================  ===================

`public` and `shared` are two of the three evasions `TRIVIAL_HELPER`'s own `fit_when` names
in `oxn.calibration`, and they are not hypothetical: the `shredding` rule clusters a function
with the *private* helpers *only it calls*, so removing either property removes the cluster.
`plump` is the third, and it fails as an evasion -- lifting every helper above the threshold
raises the cluster total instead, 13 -> 19.

What closes the gap is the class aggregate, and both forms of it separate the two escapes
from the legitimate refactoring by a wide margin: NOM 4 against 14, WMC 17 against 27. That
is the measured case for gating a class aggregate, and this file is what would notice if a
future change to `shredding` made it redundant -- in which case the aggregate is a God-Class
ceiling only, and should be argued for on those terms instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "class_scope"

#: `shredding` catches these two, and must keep catching them.
CAUGHT = ("gamed", "plump")
#: It cannot catch these: they defeat the cluster's "private" and "only it calls" premises.
ESCAPES = ("public", "shared")


def _source(variant: str) -> str:
    return (FIXTURES / f"{variant}.py.txt").read_text()


def _aggregates(variant: str) -> tuple[int, int, int]:
    """(NOM, WMC weighted by cyclomatic, max cognitive) for the `Router` class."""
    from oxn.graph.builder import build_file
    from oxn.languages import get_parser
    from oxn.metrics.engine import measure_file
    from oxn.profiles import get_profile

    profile = get_profile("python")
    data = _source(variant).encode()
    tree = get_parser("python").parse(data)
    parsed = build_file("m.py", data, profile, tree.root_node)
    measured = measure_file(list(parsed.entities), data, profile, tree.root_node)

    methods = [
        entity
        for entity in measured
        if entity.kind.value == "method" and ".Router." in f".{entity.qualified_name}."
    ]
    cyclomatic = [int(entity.values["cyclomatic_complexity"].value) for entity in methods]
    cognitive = [int(entity.values["cognitive_complexity"].value) for entity in methods]
    return len(methods), sum(cyclomatic), max(cognitive)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _check(project: Path, variant: str) -> tuple[int, set[str]]:
    from oxn.check import run_check

    (project / f"{variant}.py").write_text(_source(variant))
    report = run_check([f"{variant}.py"], use_baseline=False)
    return report.exit_code, {finding.rule for finding in report.blocking}


def test_the_honest_version_is_blocked_and_the_good_refactoring_is_not(project: Path) -> None:
    """The two ends of the control. Everything else is only meaningful against these."""
    blocked, rules = _check(project, "before")
    assert blocked != 0 and "cognitive_complexity" in rules

    passed, _ = _check(project, "spread")
    assert passed == 0, "splitting a router by HTTP verb is good design and must pass"


@pytest.mark.parametrize("variant", CAUGHT)
def test_shredding_catches_the_shapes_it_was_calibrated_on(project: Path, variant: str) -> None:
    blocked, rules = _check(project, variant)
    assert blocked != 0 and "shredding" in rules


@pytest.mark.parametrize("variant", ESCAPES)
def test_shredding_cannot_catch_a_public_or_shared_shred(project: Path, variant: str) -> None:
    """The gap, asserted so that closing it is a visible change rather than a silent one.

    If one of these starts failing, `shredding` has grown a new premise -- read why before
    deleting the case, because the class aggregate's whole justification is these two rows.
    """
    exit_code, rules = _check(project, variant)
    assert exit_code == 0, f"{variant} is now caught by {sorted(rules)}; the gap may have closed"


def test_the_class_aggregate_separates_the_escapes_from_the_refactoring() -> None:
    """Both NOM and WMC put a wide margin between good decomposition and evasion.

    The margin is what matters, not the absolute values: any ceiling inside it catches both
    escapes and passes the refactoring. `before` sits *below* both aggregates and is caught
    by the cognitive ceiling instead, which is the division of labour the pairing intends.
    """
    nom_spread, wmc_spread, _ = _aggregates("spread")
    for variant in ESCAPES:
        nom, wmc, worst = _aggregates(variant)
        assert nom > nom_spread, f"{variant}: NOM {nom} does not exceed the refactoring's"
        assert wmc > wmc_spread, f"{variant}: WMC {wmc} does not exceed the refactoring's"
        assert worst <= 12, f"{variant} should slip under the cognitive ceiling, scored {worst}"

    nom_before, wmc_before, _ = _aggregates("before")
    assert nom_before < nom_spread and wmc_before < wmc_spread


def test_wmc_is_weighted_and_is_not_a_method_count() -> None:
    """WMC == NOM in every class of every corpus until the weights were wired.

    `Evidence.complexity` existed and `test_tier3.py` covered it, but `run_classes` never
    filled it, so `sum(weights.get(name, 1) ...)` counted methods. The control makes the
    difference visible: the honest version has one method and a WMC of 14.
    """
    nom, wmc, _ = _aggregates("before")
    assert nom == 1, "the honest version is a single method"
    assert wmc > nom, f"WMC {wmc} should carry the method's complexity, not count it"
