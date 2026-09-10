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
def test_shredding_still_cannot_see_a_public_or_shared_shred(project: Path, variant: str) -> None:
    """The gap itself, which the class aggregates cover rather than remove.

    `shredding` clusters a function with the *private* helpers *only it calls*. These two
    variants defeat one premise each, so the rule has nothing to total and is silent -- and
    it should stay silent. If it starts firing here, it has grown a premise, and the class
    aggregate's justification below is worth re-reading before this case is deleted.
    """
    _, rules = _check(project, variant)
    assert "shredding" not in rules, f"{variant}: shredding now fires; its premises changed"


@pytest.mark.parametrize("variant", ESCAPES)
def test_the_class_aggregates_catch_what_shredding_cannot(project: Path, variant: str) -> None:
    """The gate closing the gap, end to end, which is what the ceilings were added for."""
    exit_code, rules = _check(project, variant)
    assert exit_code != 0, f"{variant} passed the gate"
    assert {"methods_per_class", "weighted_methods_per_class"} <= rules, sorted(rules)


def test_the_good_refactoring_stays_under_both_class_ceilings(project: Path) -> None:
    """The false-positive side of the same gate, and the reason the ceilings are not tighter.

    `spread` is the decomposition anyone would want: a router split by HTTP verb. It sits at
    NOM 4 and WMC 17 against ceilings of 12 and 25, so the margin is wide rather than lucky.
    """
    exit_code, rules = _check(project, "spread")
    assert exit_code == 0, f"the good refactoring is blocked by {sorted(rules)}"


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


# ---- the ratchet, which is how these ceilings are meant to be adopted -------------------


def test_a_baselined_class_may_stay_but_may_not_grow(project: Path) -> None:
    """Why these are a ratchet before they are a ceiling.

    NOM 12 rejects 4.3%-9.5% of classes across the five corpora, against 0.5%-3.6% for the
    per-function ceilings, because the evasions sit *inside* the legitimate distribution
    rather than beyond it. A project adopting them records what it already has and is held to
    not making it worse -- which is exactly `.oxn/baseline.json`, and is checked here because
    a ratchet nobody tested is a ceiling with extra steps.
    """
    from oxn.check import run_check, write_baseline
    from oxn.config import Config

    (project / "m.py").write_text(_source("public"))
    settings = Config.load()
    write_baseline(run_check(["m.py"], config=settings, use_baseline=False), settings.baseline_path)

    accepted = run_check(["m.py"], config=settings)
    assert accepted.exit_code == 0, "the recorded class should stop failing the build"

    worse = _source("public").replace(
        "    def route(self, request):",
        "    def extra(self, request):\n"
        "        if request:\n"
        "            return 1\n"
        "        return 0\n"
        "\n"
        "    def route(self, request):",
    )
    (project / "m.py").write_text(worse)
    assert run_check(["m.py"], config=settings).exit_code != 0, (
        "a baselined class that grows another method must fail again -- that is the ratchet"
    )


# ---- the package is only a package if all of it is present -------------------------------

_SPLIT_DECLARATION = "package split\n\ntype Widget struct {\n\tname string\n}\n\n" + (
    'func (w *Widget) Name() string { return w.name }\n'
)
_SPLIT_METHODS = "package split\n\n" + "".join(
    f"func (w *Widget) M{index}() string {{ return w.name }}\n" for index in range(12)
)


def test_a_type_whose_methods_live_in_a_sibling_file_is_counted_by_the_hook(
    project: Path,
) -> None:
    """Go's package scope was applied to whichever files the run happened to index.

    `aggregate_classes` is right that a Go type's methods can sit in a sibling file, and
    `index(targets)` handed it only the files just indexed -- on the hook path, the one file
    that was edited. So the "package" was a single-file group: `--deep` failed this tree with
    NOM 13 and the hook passed it, and the class aggregates are a *ratchet*, which makes an
    aggregate that changes with the caller's file list worse than untidy.

    Each touched directory is completed from what the cache already holds, so the sibling is
    counted without being re-parsed.
    """
    from oxn.check import run_check

    (project / "kind.go").write_text(_SPLIT_DECLARATION)
    (project / "more.go").write_text(_SPLIT_METHODS)

    whole = run_check(["."], deep=True, use_baseline=False)
    assert "methods_per_class" in {finding.rule for finding in whole.blocking}, (
        "the tree really does breach the ceiling"
    )

    hook = run_check(["kind.go"], use_baseline=False)
    assert "methods_per_class" in {finding.rule for finding in hook.blocking}, (
        "the file declaring the type must see its package's methods"
    )


def test_the_file_that_adds_the_method_is_the_one_the_gate_answers(project: Path) -> None:
    """The evasion, and the only file an agent adding a method actually edits.

    A finding is reported for the entities in the files the run was given, and the class
    entity lives where the *type* is declared. So the aggregate could be right and the edit
    still pass: `oxn check more.go` saw a package with a 13-method type and nothing to say
    about it. Moving a method to a new file was a one-line escape from a class ceiling.

    Only the class it feeds is added, never the sibling's other entities -- an unrelated
    long function over there is not this edit's problem, and the retry budget is spent on
    what the agent can act on.
    """
    from oxn.check import run_check

    (project / "kind.go").write_text(_SPLIT_DECLARATION)
    (project / "more.go").write_text(_SPLIT_METHODS)
    run_check(["."], deep=True, use_baseline=False)

    report = run_check(["more.go"], use_baseline=False)
    findings = {(finding.rule, finding.path, finding.entity) for finding in report.blocking}

    assert ("methods_per_class", "kind.go", "kind.Widget") in findings, findings
    assert all(rule == "methods_per_class" for rule, _, _ in findings), (
        "the sibling's unrelated entities must not be dragged in"
    )
