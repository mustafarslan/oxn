"""P7's exit criterion, as a test: the rules reproduce the hand-coded suite exactly.

The hand-coded checks are the oracle here, and they stay: this test is what gives them a
job, so deleting them deletes the property rather than the duplication (ADR-0005, amended
2026-09-04). Parity means the same findings with the same identity -- the
baseline is keyed `rule|path|entity`, so a rule engine producing equivalent findings under
different keys would silently reset every user's accepted debt.

The fixture below violates all four contract kinds *and* keeps one legal edge, because a
rule that fires on everything passes a parity test just as well as a correct one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oxn.check import CheckReport, _architecture, _measure
from oxn.config import Config
from oxn.graph.contracts import assign_layers
from oxn.graph.depgraph import build_dependency_graph
from oxn.graph.indexer import Indexer
from oxn.graph.sources import iter_source_files
from oxn.rules.builtin import all_rules
from oxn.rules.engine import evaluate
from oxn.rules.facts import file_facts, graph_facts

CONFIG = """
ceilings:
  cognitive_complexity: 12
  cyclomatic_complexity: 10
  max_nesting_depth: 4
  parameter_count: 5
  function_sloc: 60
  file_sloc: 500
layers:
  ui: ["src/ui/*"]
  app: ["src/app/*"]
  domain: ["src/domain/*"]
  infra: ["src/infra/*"]
  secret: ["src/secret/*"]
contracts:
  - name: clean
    kind: layered
    order: [ui, app, domain]
  - name: no-infra-from-domain
    kind: forbidden
    source: domain
    forbidden: [infra]
  - name: keep-apart
    kind: independence
    modules: [ui, infra]
  - name: secret-door
    kind: deep_import
    package: secret
    allowed_entrypoints: ["src/secret/api.py"]
"""

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


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A tree that violates all four contract kinds, two ceilings, and nothing else."""
    (tmp_path / "oxn.yaml").write_text(CONFIG)
    for package in ("ui", "app", "domain", "infra", "secret"):
        directory = tmp_path / "src" / package
        directory.mkdir(parents=True)
        (directory / "__init__.py").write_text("")
    (tmp_path / "src" / "__init__.py").write_text("")

    src = tmp_path / "src"
    # domain -> app is uphill; domain -> infra is forbidden; ui -> infra breaks independence.
    (src / "domain" / "d.py").write_text("from src.app import a\nfrom src.infra import i\n")
    (src / "ui" / "u.py").write_text("from src.infra import i\n")
    # One deep import and one through the declared door, so the door is tested too.
    (src / "app" / "a.py").write_text(
        "from src.secret.internal import x\nfrom src.secret.api import y\n" + TANGLED
    )
    (src / "infra" / "i.py").write_text("")
    (src / "secret" / "api.py").write_text("")
    (src / "secret" / "internal.py").write_text("")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _identity(finding) -> tuple:
    """Everything a baseline and a rendered message depend on."""
    return (
        finding.rule,
        finding.path,
        finding.entity,
        finding.line,
        round(finding.value, 6),
        round(finding.ceiling, 6),
        finding.blocking,
    )


def _hand_coded(settings: Config) -> list:
    report = CheckReport()
    targets = [Path(".")]
    return _measure(targets, settings, report) + _architecture(targets, settings, report)


def _from_rules(settings: Config) -> list:
    targets = [Path(".")]
    with Indexer() as indexer:
        indexer.index(targets)
        wanted = [indexer.relative(path) for path in iter_source_files(targets)]
        layers = assign_layers(wanted, settings.layers) if settings.layers else {}
        facts = file_facts(indexer.store, wanted, settings, layers)
        if settings.contracts:
            graph = build_dependency_graph(indexer.root, list(iter_source_files(targets)))
            graph_facts(facts, graph, settings)
    return evaluate(all_rules(settings), facts)


def test_the_rules_reproduce_the_hand_coded_findings_exactly(project) -> None:
    settings = Config.load(project)
    old = sorted(map(_identity, _hand_coded(settings)))
    new = sorted(map(_identity, _from_rules(settings)))
    assert old == new
    assert len(old) > 5, f"fixture produced only {len(old)} findings; parity would be vacuous"


def test_the_fixture_exercises_every_contract_kind(project) -> None:
    """A parity test on a tree with no violations passes trivially, which is worthless."""
    settings = Config.load(project)
    rules = {finding.rule for finding in _from_rules(settings)}
    assert {
        "contract:clean",
        "contract:no-infra-from-domain",
        "contract:keep-apart",
        "contract:secret-door",
    } <= rules
    assert "cognitive_complexity" in rules


def test_the_declared_entry_point_is_not_a_violation(project) -> None:
    """`deep_import` must permit the door it declares, or it is just `forbidden`."""
    settings = Config.load(project)
    doors = {
        finding.entity
        for finding in _from_rules(settings)
        if finding.rule == "contract:secret-door"
    }
    assert any("secret/internal.py" in entity for entity in doors)
    assert not any("secret/api.py" in entity for entity in doors)


def test_findings_keep_the_identity_the_baseline_is_built_on(project) -> None:
    """`rule|path|entity`, unchanged -- otherwise every user's accepted debt resets."""
    settings = Config.load(project)
    old = {(f.rule, f.path, f.entity) for f in _hand_coded(settings)}
    new = {(f.rule, f.path, f.entity) for f in _from_rules(settings)}
    assert old == new


# ---- the ratchet must survive the engine change ------------------------------------------


def test_a_baseline_written_by_the_hand_coded_gate_reads_clean_under_the_rules(project) -> None:
    """The property the whole cutover depends on, and the reason identity is specified.

    A user upgrades and their accepted debt must stay accepted. If the rule engine keyed
    findings differently -- even producing exactly the same violations -- every baselined
    entry would read as new, the build would fail everywhere, and the obvious fix would be
    to regenerate: an amnesty dressed as an upgrade.
    """
    from oxn.check import run_check, write_baseline

    settings = Config.load(project)

    # A baseline recorded from the hand-coded findings, exactly as `oxn baseline` would.
    report = CheckReport(scope="repository")
    report.findings = _hand_coded(settings)
    written = write_baseline(report, settings.baseline_path)
    assert written > 5, "a baseline this small would not prove anything"

    # Now the rule-driven gate reads it back on an unchanged tree.
    after = run_check(["."], config=settings, deep=True, use_baseline=True)
    assert after.regressed == [], [str(f) for f in after.regressed]
    assert after.findings == [], [str(f) for f in after.findings]
    assert after.passed
