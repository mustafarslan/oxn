"""Tier-2 architecture metrics and conformance, against constructed fixtures.

Arcan's published smell labels are for Java corpora, and no equivalent labelled corpus
exists for Python or TypeScript. So correctness is established on graphs whose answers can
be computed by hand, and behaviour on real code is *characterised* in docs/divergences.md
rather than asserted against labels that do not exist.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from oxn.graph.architecture import LakosMetrics, analyse, detect_smells
from oxn.graph.contracts import Contract, Layer, assign_layers, check_contracts

# ---- Martin metrics ---------------------------------------------------------------------


def test_instability_of_a_pure_consumer_is_one() -> None:
    graph = {"consumer": {"library"}, "library": set()}
    report = analyse(graph)
    assert report.martin["consumer"].instability == 1.0
    assert report.martin["library"].instability == 0.0


def test_instability_is_efferent_over_total() -> None:
    graph = {"a": {"b", "c"}, "b": {"c"}, "c": set(), "d": {"a"}}
    report = analyse(graph)
    a = report.martin["a"]
    assert (a.afferent, a.efferent) == (1, 2)
    assert a.instability == pytest.approx(2 / 3)


def test_isolated_component_is_stable_by_convention() -> None:
    assert analyse({"lonely": set()}).martin["lonely"].instability == 0.0


def test_abstractness_is_none_without_types() -> None:
    """Reporting 0.0 would place a type-free component at the concrete end of the main
    sequence purely for having no classes, which is a false signal."""
    report = analyse({"a": set()}, types={})
    assert report.martin["a"].abstractness is None
    assert report.martin["a"].distance is None


def test_distance_from_the_main_sequence() -> None:
    # Fully abstract and fully stable: A=1, I=0 -> D=0, right on the sequence.
    report = analyse({"a": set()}, types={"a": (4, 4)})
    assert report.martin["a"].abstractness == 1.0
    assert report.martin["a"].distance == pytest.approx(0.0)

    # Concrete and stable: A=0, I=0 -> D=1, the "zone of pain".
    report = analyse({"a": set()}, types={"a": (0, 4)})
    assert report.martin["a"].distance == pytest.approx(1.0)


# ---- Lakos -------------------------------------------------------------------------------


def test_ccd_of_a_chain_is_triangular() -> None:
    """A three-link chain: each component depends on itself plus everything downstream."""
    graph = {"a": {"b"}, "b": {"c"}, "c": set()}
    report = analyse(graph)
    assert report.lakos is not None
    assert report.lakos.ccd == 3 + 2 + 1
    assert report.lakos.acd == pytest.approx(2.0)


def test_nccd_compares_against_a_balanced_binary_tree() -> None:
    metrics = LakosMetrics(ccd=10, component_count=7)
    expected = (7 + 1) * math.log2(8) - 7
    assert metrics.ccd_balanced_binary_tree == pytest.approx(expected)
    assert metrics.nccd == pytest.approx(10 / expected)


def test_a_cycle_inflates_ccd() -> None:
    """Everything in a cycle depends on everything else in it -- which is the point."""
    acyclic = analyse({"a": {"b"}, "b": {"c"}, "c": set()})
    cyclic = analyse({"a": {"b"}, "b": {"c"}, "c": {"a"}})
    assert cyclic.lakos.ccd > acyclic.lakos.ccd
    assert cyclic.lakos.ccd == 9  # every one of three reaches all three


def test_levels_follow_the_dependency_depth() -> None:
    report = analyse({"a": {"b"}, "b": {"c"}, "c": set()})
    assert report.levels == {"a": 2, "b": 1, "c": 0}


# ---- propagation cost --------------------------------------------------------------------


def test_propagation_cost_of_independent_components_is_minimal() -> None:
    """N isolated components: each reaches only itself, so the density is 1/N."""
    report = analyse({name: set() for name in "abcd"})
    assert report.propagation_cost == pytest.approx(4 / 16)


def test_propagation_cost_of_a_fully_connected_graph_is_one() -> None:
    names = list("abc")
    graph = {name: {other for other in names if other != name} for name in names}
    assert analyse(graph).propagation_cost == pytest.approx(1.0)


# ---- smells ------------------------------------------------------------------------------


def smells_of(report, kind: str):
    return [smell for smell in report.smells if smell.kind == kind]


def test_cyclic_dependency_is_detected() -> None:
    report = analyse({"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": set()})
    found = smells_of(report, "cyclic_dependency")
    assert len(found) == 1
    assert set(found[0].members) == {"a", "b", "c"}


def test_hub_like_dependency_needs_balance_not_just_popularity() -> None:
    """A component everything imports is not a hub; a hub also imports everything."""
    popular = {"hub": set()} | {f"c{i}": {"hub"} for i in range(8)}
    assert smells_of(analyse(popular), "hub_like_dependency") == []

    balanced = {"hub": {f"out{i}" for i in range(6)}}
    balanced |= {f"in{i}": {"hub"} for i in range(6)}
    balanced |= {f"out{i}": set() for i in range(6)}
    found = smells_of(analyse(balanced), "hub_like_dependency")
    assert [smell.component for smell in found] == ["hub"]


def test_unstable_dependency_is_detected() -> None:
    """A stable component depending on less stable ones violates Martin's SDP.

    `core` is depended on by three components and depends on two, so its instability is
    low; both of its dependencies depend on things while nothing depends on them, so theirs
    is maximal. Depending *downhill* in stability is the smell.
    """
    graph = {
        "core": {"volatile_a", "volatile_b"},
        "volatile_a": {"leaf"},
        "volatile_b": {"leaf"},
        "leaf": set(),
        "user_a": {"core"},
        "user_b": {"core"},
        "user_c": {"core"},
    }
    report = analyse(graph)
    assert report.martin["core"].instability < report.martin["volatile_a"].instability
    found = smells_of(report, "unstable_dependency")
    assert "core" in {smell.component for smell in found}


def test_god_component_uses_the_floor_when_there_are_too_few_components() -> None:
    """A percentile over three points is not a percentile: the p90 *is* the maximum, so
    nothing could ever exceed it. Small systems fall back to the published floor."""
    sizes = {"huge": 20_000, "small": 100, "medium": 400}
    graph = {name: set() for name in sizes}
    found = smells_of(analyse(graph, sizes=sizes), "god_component")
    assert [smell.component for smell in found] == ["huge"]


def test_god_component_uses_the_percentile_when_there_are_enough_components() -> None:
    sizes = {f"c{index}": 100 for index in range(12)} | {"huge": 90_000}
    graph = {name: set() for name in sizes}
    found = smells_of(analyse(graph, sizes=sizes), "god_component")
    assert [smell.component for smell in found] == ["huge"]


def test_no_smells_in_a_clean_layered_graph() -> None:
    graph = {"web": {"service"}, "service": {"repo"}, "repo": set()}
    assert analyse(graph, sizes={"web": 100, "service": 120, "repo": 90}).smells == []


# ---- conformance --------------------------------------------------------------------------


LAYERS = [
    Layer("infrastructure", ("src/infra/*",)),
    Layer("application", ("src/app/*",)),
    Layer("domain", ("src/domain/*",)),
]
LAYERED = Contract(name="clean", kind="layered", order=("infrastructure", "application", "domain"))


def test_layers_are_assigned_by_glob() -> None:
    assignment = assign_layers(["src/app/a.py", "src/domain/b.py", "scripts/c.py"], LAYERS)
    assert assignment == {
        "src/app/a.py": "application",
        "src/domain/b.py": "domain",
        "scripts/c.py": None,
    }


def test_a_conforming_graph_passes() -> None:
    graph = {
        "src/infra/db.py": {"src/app/svc.py"},
        "src/app/svc.py": {"src/domain/order.py"},
        "src/domain/order.py": set(),
    }
    report = check_contracts(graph, LAYERS, [LAYERED])
    assert report.passed
    assert report.convergent == 2


def test_an_inward_layer_reaching_outward_is_a_violation() -> None:
    graph = {
        "src/domain/order.py": {"src/infra/db.py"},
        "src/infra/db.py": set(),
    }
    report = check_contracts(graph, LAYERS, [LAYERED])
    assert not report.passed
    assert report.divergent[0].source == "domain"
    assert report.divergent[0].target == "infrastructure"


def test_a_violation_carries_the_import_chain_that_proves_it() -> None:
    """ "domain depends on infrastructure" is not actionable; the chain names the edge.

    The chain starts at the file that actually crosses the boundary -- the one to change --
    not at some earlier file that merely reaches it.
    """
    graph = {
        "src/domain/order.py": {"src/domain/repo.py"},
        "src/domain/repo.py": {"src/infra/db.py"},
        "src/infra/db.py": set(),
    }
    report = check_contracts(graph, LAYERS, [LAYERED])
    assert report.divergent[0].chain == ("src/domain/repo.py", "src/infra/db.py")


def test_an_indirect_violation_reports_every_hop() -> None:
    """When the boundary is crossed through an intermediary, the route shows the hops."""
    graph = {
        "src/domain/order.py": {"src/app/helper.py"},
        "src/app/helper.py": {"src/infra/db.py"},
        "src/infra/db.py": set(),
    }
    report = check_contracts(graph, LAYERS, [LAYERED])
    chains = {violation.chain for violation in report.divergent}
    assert ("src/domain/order.py", "src/app/helper.py") in chains


def test_forbidden_contract() -> None:
    graph = {"src/domain/a.py": {"src/infra/b.py"}, "src/infra/b.py": set()}
    contract = Contract(
        name="no-infra", kind="forbidden", source="domain", forbidden=("infrastructure",)
    )
    assert not check_contracts(graph, LAYERS, [contract]).passed


def test_independence_contract_is_symmetric() -> None:
    graph = {"src/app/a.py": {"src/domain/b.py"}, "src/domain/b.py": set()}
    contract = Contract(name="split", kind="independence", modules=("application", "domain"))
    assert not check_contracts(graph, LAYERS, [contract]).passed


def test_deep_import_contract_allows_declared_entrypoints() -> None:
    graph = {
        "src/app/ok.py": {"src/domain/ports.py"},
        "src/app/bad.py": {"src/domain/internal.py"},
        "src/domain/ports.py": set(),
        "src/domain/internal.py": set(),
    }
    contract = Contract(
        name="ports-only",
        kind="deep_import",
        package="domain",
        allowed_entrypoints=("src/domain/ports.py",),
    )
    report = check_contracts(graph, LAYERS, [contract])
    assert [violation.target for violation in report.divergent] == ["src/domain/internal.py"]


def test_absent_edges_surface_stale_rules() -> None:
    """A declared dependency nothing exercises is usually a rule that stopped meaning something."""
    graph = {"src/app/a.py": {"src/domain/b.py"}, "src/domain/b.py": set()}
    report = check_contracts(graph, LAYERS, [LAYERED])
    assert ("infrastructure", "application") in report.absent


def test_files_outside_every_layer_are_reported() -> None:
    graph = {"scripts/tool.py": set()}
    assert check_contracts(graph, LAYERS, [LAYERED]).unassigned == ["scripts/tool.py"]


# ---- reports must be diffable ---------------------------------------------------------


def _peers_graph(order: list[str]) -> dict[str, set[str]]:
    """Symmetric peers, each with its own volatile dependencies.

    Each peer is depended on by three users (low instability) and depends on two components
    that depend on three leaves and nothing depends on (high instability), so every peer is
    an equally severe Unstable Dependency. They differ only by name.
    """
    graph: dict[str, set[str]] = {}
    for peer in order:
        graph[peer] = {f"{peer}_v1", f"{peer}_v2"}
        for volatile in (f"{peer}_v1", f"{peer}_v2"):
            graph[volatile] = {"l1", "l2", "l3"}
        for user in ("u1", "u2", "u3"):
            graph.setdefault(user, set()).add(peer)
    for leaf in ("l1", "l2", "l3"):
        graph[leaf] = set()
    return graph


def test_equally_severe_smells_come_back_in_a_stable_order() -> None:
    """`oxn arch` must produce the same bytes for the same code, every run.

    Found by a differential test that was checking something else: three consecutive runs
    of `oxn arch --json` over an unchanged corpus produced three different outputs. The
    smell sort keyed on `(kind, -severity)` only, so components tying on both -- the common
    case, since severity is a coarse ratio -- fell back to insertion order, and insertion
    order came out of set iteration, which varies with the per-process hash seed.

    A report that cannot be diffed against the previous commit cannot show a trend, and
    trend is most of what a report is for.

    The peers are inserted in reverse-alphabetical order deliberately. Without the
    tie-break they come back in that order; with it they come back sorted, so this test
    fails against the code that shipped before it.
    """
    graph = _peers_graph(["zeta", "yankee", "xray"])
    smells = detect_smells(graph, analyse(graph), dict.fromkeys(graph, 10))
    tied = [smell.component for smell in smells if smell.kind == "unstable_dependency"]
    assert tied == ["xray", "yankee", "zeta"], "equal severities must break ties by name"

    rows = [(smell.kind, -smell.severity, smell.component) for smell in smells]
    assert rows == sorted(rows), "the sort key must be total, or the order is hash-dependent"


# ---- acyclic, the fifth contract kind ------------------------------------------------------


def _cycle_project(root: Path, *, deferred: bool) -> None:
    """Two packages that import each other; `deferred` puts one import inside a function."""
    for package in ("alpha", "beta"):
        (root / package).mkdir()
        (root / package / "__init__.py").write_text("")
    (root / "alpha" / "core.py").write_text(
        "from beta import helper\n\n\ndef go():\n    return helper.value()\n"
    )
    back = (
        "def value():\n    return 1\n\n\ndef back():\n    from alpha import core\n\n"
        "    return core.go()\n"
        if deferred
        else "from alpha import core\n\n\ndef value():\n    return 1\n\n\n"
        "def back():\n    return core.go()\n"
    )
    (root / "beta" / "helper.py").write_text(back)


def _acyclic_check(root: Path, *, strict: bool = False):
    from oxn.check import run_check

    (root / "oxn.yaml").write_text(
        "version: 1\nlayers:\n  alpha: ['alpha/**']\n  beta: ['beta/**']\n"
        "contracts:\n  - name: no-rings\n    kind: acyclic\n"
        + ("    deferred: true\n" if strict else "")
    )
    return run_check(["."], deep=True, use_baseline=False)


def test_an_acyclic_contract_blocks_a_ring_between_two_packages(tmp_path, monkeypatch) -> None:
    """The gate could not answer this at all until 2026-09-10.

    `cycle` was declared in `REPOSITORY_RELATIONS`, `--deep` advertised it in its own
    diagnostic, and no fact source produced a row: a two-package ring passed with 0
    violations while `oxn arch` reported it.
    """
    monkeypatch.chdir(tmp_path)
    _cycle_project(tmp_path, deferred=False)

    report = _acyclic_check(tmp_path)
    assert report.exit_code != 0
    assert "contract:no-rings" in {finding.rule for finding in report.blocking}


def test_a_cycle_that_only_exists_through_a_deferred_import_is_not_one(
    tmp_path, monkeypatch
) -> None:
    """Moving the import into the function *is* the fix, in Python and in CommonJS.

    A check that counts it reports the remedy as the fault. Measured on OXN's own tree: 9
    components in a ring on all imports, 2 on the ones that run at module load.
    """
    monkeypatch.chdir(tmp_path)
    _cycle_project(tmp_path, deferred=True)

    assert _acyclic_check(tmp_path).exit_code == 0
    assert _acyclic_check(tmp_path, strict=True).exit_code != 0, "`deferred: true` still asks"


def test_the_hook_declines_a_cycle_rather_than_answering_it_from_one_file(
    tmp_path, monkeypatch
) -> None:
    """An SCC is a property of the whole graph; a file-scoped run can only get it wrong."""
    from oxn.check import run_check

    monkeypatch.chdir(tmp_path)
    _cycle_project(tmp_path, deferred=False)
    _acyclic_check(tmp_path)

    assert run_check(["alpha/core.py"], use_baseline=False).exit_code == 0


def test_a_misspelled_contract_kind_is_refused_rather_than_silently_inert(tmp_path) -> None:
    """`contract_rules` skips what it does not recognise, so `kind: acylic` gated nothing.

    A gate that quietly does not run is worse than one that is switched off: the second is
    a decision and the first reads like protection.
    """
    from oxn.config import Config, ConfigError

    (tmp_path / "oxn.yaml").write_text(
        "version: 1\ncontracts:\n  - name: rings\n    kind: acylic\n"
    )
    with pytest.raises(ConfigError) as raised:
        Config.load(tmp_path)
    assert "acylic" in str(raised.value) and "acyclic" in str(raised.value)


# ---- core / periphery ----------------------------------------------------------------------


def _ring(size: int, extras: dict[str, set[str]] | None = None) -> dict[str, set[str]]:
    """A cycle of `size` components, plus whatever else is named."""
    graph: dict[str, set[str]] = {f"c{index}": {f"c{(index + 1) % size}"} for index in range(size)}
    for name, targets in (extras or {}).items():
        graph.setdefault(name, set()).update(targets)
        for target in targets:
            graph.setdefault(target, set())
    return graph


def test_the_four_roles_come_out_where_a_reader_would_put_them() -> None:
    """`shared` is reached by everything and reaches nothing; `control` is the reverse.

    Measured on OXN's own tree, which is the case this was written against: `src/oxn/profiles`
    is shared (fan-in 12, fan-out 1), `scripts` and `tests` are control (1 and 11), and the
    nine components of its ring are the core.
    """
    graph = _ring(10, {"entry": {"c0"}, "util": set()})
    graph["c0"].add("util")

    found = analyse(graph).visibility

    assert {name: seen.role for name, seen in found.items()}["util"] == "shared"
    assert found["entry"].role == "control"
    assert all(found[f"c{index}"].role == "core" for index in range(10))


def test_a_cycle_ties_every_member_and_the_threshold_must_survive_that() -> None:
    """The bug this method replaced, and the reason MacCormack uses the cyclic group.

    Every member of a cycle reaches and is reached by every other, so all of them carry
    identical visibility -- and a median lands exactly on that tie. On OXN's own tree the
    nine ring members took fan-in 11 and fan-out 10 against medians of 11 and 10, and a
    strict `>` filed **the entire core under `peripheral`**: the most misleading label
    available, on the components that matter most.
    """
    found = analyse(_ring(11, {"entry": {"c0"}})).visibility
    ring = [found[f"c{index}"] for index in range(11)]

    assert len({(seen.fan_in, seen.fan_out) for seen in ring}) == 1, "a ring ties by definition"
    assert {seen.role for seen in ring} == {"core"}


def test_visibility_is_refused_on_a_tree_too_small_to_have_a_structure() -> None:
    """A threshold over four components is a coin toss; the paper's systems had ~20,000 files."""
    assert analyse({"a": {"b"}, "b": {"c"}, "c": set()}).visibility == {}


def test_fan_in_and_fan_out_are_reflexive_and_agree_with_propagation_cost() -> None:
    """Propagation cost is the density of the same matrix, so the two must not disagree.

    If they were counted under different conventions -- one reflexive, one not -- the report
    would carry two numbers about one structure that could never be reconciled by a reader.
    """
    report = analyse(_ring(10, {"entry": {"c0"}}))
    total = len(report.components)
    reachable = sum(seen.fan_out for seen in report.visibility.values())

    assert report.propagation_cost == pytest.approx(reachable / (total * total))
    assert all(seen.fan_out >= 1 for seen in report.visibility.values()), "reflexive"
