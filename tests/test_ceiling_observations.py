"""The measured cost of each gated ceiling, and the prose that quotes it.

P10 asked for the ceilings to stop being `judgement n=0`. What `scripts/measure_ceilings.py`
found is that the method P10 named -- fit each ceiling to a corpus percentile -- does not
survive contact with the distributions: they have median 0, so the 95th percentile of
cognitive complexity is 2 in TypeScript and 9 in Go. The ceilings therefore did not move, and
what they gained instead is an *exceedance* figure quoted in `calibration.py`.

A number quoted in prose rots exactly like the roadmap sentence that read "9 of 9 are
provisional" three parameters after the ninth. These tests hold the quoted figures to the
frozen measurement, which is why the measurement is a committed file rather than something
the corpora have to be present to reproduce.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

OBSERVATIONS = Path(__file__).resolve().parent.parent / "benchmarks" / "ceiling-observations.json"


@pytest.fixture(scope="module")
def observed() -> dict[str, dict]:
    return json.loads(OBSERVATIONS.read_text())["observed"]


def test_every_gated_ceiling_was_measured(observed: dict[str, dict]) -> None:
    """A ceiling absent from the measurement is a ceiling nobody knows the cost of."""
    from oxn.config import GATED_METRICS

    # `shredding` shares the cognitive ceiling rather than owning a number, so it has no
    # distribution of its own -- only cluster roots carry the metric at all.
    gated = set(GATED_METRICS) - {"shredding"}
    assert gated == set(observed), f"unmeasured: {sorted(gated - set(observed))}"


def test_the_measurement_used_the_ceilings_that_are_actually_enforced(
    observed: dict[str, dict],
) -> None:
    """Freezing a measurement against a ceiling that has since moved measures nothing."""
    from oxn import thresholds
    from oxn.config import GATED_METRICS

    for rule, record in observed.items():
        live = float(getattr(thresholds, GATED_METRICS[rule].threshold))
        assert record["ceiling"] == live, (
            f"{rule} was measured against {record['ceiling']} but is enforced at {live}; "
            "re-run scripts/measure_ceilings.py"
        )


#: Ceilings whose population is *every* entity of the gated kind rather than the named ones.
#:
#: For callables, `named` is the comparable row: anonymous density runs from 0.1% of httpx's
#: to 69.4% of nest's, so it is the only row two languages can be compared on. For classes the
#: same filter measures the wrong thing. Go is the starkest case and is unaffected by anything
#: since: all 379 of go-kit's structs are unnamed entities, so the named row does not exist. Rust
#: made the same argument at measurement time -- an `impl` block is an unnamed class entity
#: holding every method of its type, and ripgrep read 840 against 395 named, its exceedance
#: falling from 3.10% to 0.25% under the filter -- but the `impl` join has since moved those
#: methods onto the named type. The gate applies no name filter, so neither does this.
#:
#: Nothing here re-measures: these tests hold the frozen record against the live ceilings and
#: against `calibration.py`, so they cannot notice a record that has gone stale. That is not
#: hypothetical -- it happened, twice over. The record predated the `impl` join and four
#: receiver fixes while every test here passed, and the measurement script itself was reading
#: corpus caches two schema versions behind and reporting them as current. The script refuses
#: a mismatched schema now; re-running it remains the only thing that can refresh this.
_WHOLE_POPULATION = frozenset({"methods_per_class", "weighted_methods_per_class"})


def test_calibration_quotes_the_population_it_measured(observed: dict[str, dict]) -> None:
    """`observations` must be the count behind the sentence next to it, not a round number."""
    from oxn.calibration import parameters
    from oxn.config import GATED_METRICS

    by_threshold = {GATED_METRICS[rule].threshold: rule for rule in observed}
    for parameter in parameters():
        rule = by_threshold.get(parameter.name)
        if rule is None:
            continue
        population = "all_callables" if rule in _WHOLE_POPULATION else "named"
        measured = sum(
            corpus[population]["n"]
            for corpus in observed[rule]["corpora"].values()
            if population in corpus
        )
        assert parameter.observations == measured, (
            f"{parameter.name} claims {parameter.observations} observations; the frozen "
            f"measurement has {measured}"
        )


def test_a_percentile_would_not_have_been_a_ceiling(observed: dict[str, dict]) -> None:
    """The finding that stopped P10 fitting these, asserted rather than only written down.

    If a future change to the metrics made these distributions unimodal and centred, the
    percentile route would deserve reopening -- and this test failing is how that gets
    noticed, instead of the argument in `calibration.py` quietly becoming false.
    """
    cognitive = observed["cognitive_complexity"]["corpora"]
    p95 = {
        language: corpus["all_callables"]["quantiles"]["p95"]
        for language, corpus in cognitive.items()
    }
    assert max(p95.values()) < 12, (
        f"p95 cognitive complexity is now {p95}; every corpus still sits far below the "
        "ceiling of 12, so a p95-fitted ceiling would reject ordinary code"
    )
    medians = {
        language: corpus["all_callables"]["quantiles"]["p50"]
        for language, corpus in cognitive.items()
    }
    assert set(medians.values()) == {0}, f"medians are no longer all zero: {medians}"


def test_file_sloc_is_the_ceiling_the_measurement_argues_with(observed: dict[str, dict]) -> None:
    """The one finding worth acting on, pinned so that acting on it is a deliberate edit.

    Every other ceiling rejects a comparable slice of every language. `MAX_FILE_SLOC` rejects
    a fiftieth of a percent of go-kit and a fifth of ripgrep, and the Rust files above it are
    hand-written core rather than generated. Held at 500 on one repository per language; this
    fails if a second Rust corpus changes the picture, which is exactly when to look again.
    """
    exceedance = {
        name: corpus["all_callables"]["exceedance"]
        for name, corpus in observed["file_sloc"]["corpora"].items()
    }
    assert exceedance["rust-ripgrep"] > 15.0, f"ripgrep moved to {exceedance['rust-ripgrep']}"
    assert exceedance["go-kit"] < 1.0, f"go-kit moved to {exceedance['go-kit']}"


# ---- what the class ceilings reject, in code nobody wrote for them ----------------------


@pytest.fixture(scope="module")
def audit() -> dict[str, int]:
    return json.loads(OBSERVATIONS.read_text())["class_ceiling_audit"]


def test_neither_class_ceiling_is_redundant(audit: dict[str, int]) -> None:
    """Each rejects classes the other accepts, so both earn their place.

    Positioned by one authored control, both ceilings would be exactly the circularity
    `TRIVIAL_HELPER`'s `fit_when` warns about. This is the other half of the evidence: a
    census of the five corpora, which nobody wrote to be caught. If either count reaches
    zero, that ceiling has become a restatement of the other and should be removed rather
    than kept for symmetry.
    """
    assert audit["nom_only"] > 0, "every NOM rejection is also a WMC rejection"
    assert audit["wmc_only"] > 0, "every WMC rejection is also a NOM rejection"


def test_the_weighted_ceiling_sees_what_no_per_function_gate_can(audit: dict[str, int]) -> None:
    """The God Class shape: every method fine on its own, the accumulation the problem.

    `GraphStore` is the local example -- WMC 64 with a worst method of cyclomatic 6 -- and it
    is the majority case, not a curiosity. If this fell to zero, `weighted_methods_per_class`
    would be catching only classes `cyclomatic_complexity` already blocks.
    """
    over_wmc = audit["both"] + audit["wmc_only"]
    assert audit["wmc_without_a_hot_method"] > over_wmc // 2, (
        f"only {audit['wmc_without_a_hot_method']} of the {over_wmc} classes over the WMC "
        "ceiling are invisible to the per-function ceilings; the gate may be redundant"
    )


#: Rejections in test files, and rejections in total. Measured over seven corpora.
#:
#: **This was an inequality -- `in_test_files <= rejected // 10` -- and a seventh corpus
#: refuted it.** Over six it read 8 of 129, six percent, and "small" was a fair description.
#: Adding `python-airflow`, 130x the Python sample and carrying a test suite to match, took
#: it to 867 of 1,425: the class ceilings reject *mostly test code* at real-repository scale.
#:
#: The inequality is gone rather than loosened, because a bound chosen when the true value
#: was 6% says nothing once the true value is 61% -- raising it to fit would be fitting the
#: threshold to the observation and calling it a check. A pinned pair asserts the same thing
#: the inequality was for, which is that a change in this cost is visible in a diff, and
#: asserts it without claiming a size the evidence contradicts.
#:
#: What the number means has not changed: many small test methods is a legitimate shape, and
#: `oxn.yaml`'s `exclude` and advisory paths are how a project opts its tests out. What has
#: changed is how much that answer matters -- it is now most of the ceiling's cost.
RECORDED_TEST_FILE_REJECTIONS = (872, 1512)


def test_the_known_false_positive_shape_is_recorded(audit: dict[str, int]) -> None:
    """A test class with many small test methods is a legitimate shape, and NOM rejects it."""
    measured = (audit["in_test_files"], audit["rejected"])
    assert measured == RECORDED_TEST_FILE_REJECTIONS, (
        f"{measured[0]} of {measured[1]} rejections are in test files, against a record of "
        f"{RECORDED_TEST_FILE_REJECTIONS[0]} of {RECORDED_TEST_FILE_REJECTIONS[1]}. If a "
        "corpus moved or a ceiling changed, update the record in the same commit and say "
        "which -- this is the ceiling's cost, not an incidental count."
    )


# ---- what the measurement refuses to measure ---------------------------------------------


def _measure_ceilings():
    """`scripts/` is not a package, so the module is loaded by path.

    Registered in `sys.modules` **before** it is executed, which is what the import system
    does and this loader did not: a `@dataclass(slots=True)` rebuilds its class and looks
    itself up by `__module__` to do it, so declaring one in a script loaded this way failed
    with a bare `AttributeError` from inside `dataclasses`. The same shape breaks pickling
    and `typing.get_type_hints`, so the fix belongs in the loader rather than in the script.
    """
    import importlib.util
    import sys

    path = Path(__file__).resolve().parent.parent / "scripts" / "measure_ceilings.py"
    spec = importlib.util.spec_from_file_location("measure_ceilings", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_a_cache_holding_part_of_the_tree_is_not_a_measurement(tmp_path: Path) -> None:
    """Schema alone was the check, and a schema-current cache can still answer for a third.

    Measured on 2026-09-10: the corpora held 23 of httpx's 60 files, 901 of nest's 1,913 and
    30 of petclinic's 50, all stamped with the current schema, because a cache is written by
    whoever opened it and anything that indexes a subset in place leaves one. Freezing that
    would have put a distribution over 4,000 callables into `calibration.py` as one over
    10,000 -- the schema-7 incident's family, one level down, and just as invisible to a
    green suite.
    """
    from oxn.graph.indexer import Indexer

    for index in range(4):
        (tmp_path / f"m{index}.py").write_text(f"def f{index}(x):\n    return x\n")
    cache = tmp_path / ".oxn" / "cache" / "graph.db"
    cache.parent.mkdir(parents=True)

    module = _measure_ceilings()
    with Indexer(root=tmp_path, cache_path=cache) as indexer:
        indexer.index([tmp_path / "m0.py"])
    assert module._coverage(tmp_path, cache) == (1, 4), "a partial cache must report as one"

    with Indexer(root=tmp_path, cache_path=cache) as indexer:
        indexer.index()
    assert module._coverage(tmp_path, cache) == (4, 4)


#: Declared `use: threshold` in the manifest and absent from the frozen measurement, with the
#: reason. An entry here is a claim that the gap is known, not that it does not matter.
UNMEASURED: dict[str, str] = {}
#: Empty since 2026-09-18, when `python-airflow` was fetched and measured -- it was the one
#: entry, and it had been a silent absence for long enough that three different corpus counts
#: were in circulation. An entry here is a claim that a gap is known, not that it is
#: acceptable, so the right end state is this dictionary staying empty.


def test_the_frozen_measurement_covers_every_threshold_corpus_or_names_the_gap() -> None:
    """**The corpus count is a quoted number too, and it had drifted.**

    `docs/metrics.md` and `scripts/measure_ceilings.py` both said "five `use: threshold`
    corpora" while the frozen file held six and the manifest declared seven -- three numbers
    for one set. `python-airflow` is declared and has never been fetched, so every figure in
    this measurement is over six repositories and the seventh is a silent absence.

    The manifest is the declaration and the frozen file is the evidence; when they disagree the
    difference has to be named here, which is the only way a corpus can go missing loudly.
    """
    yaml = pytest.importorskip("yaml")

    manifest = yaml.safe_load((OBSERVATIONS.parent / "manifest.yaml").read_text())
    declared = {
        corpus["name"] for corpus in manifest["corpora"] if corpus.get("use") == "threshold"
    }
    frozen = set(json.loads(OBSERVATIONS.read_text())["corpora"])

    assert frozen <= declared, (
        f"measured a corpus the manifest does not declare: {frozen - declared}"
    )
    assert declared - frozen == set(UNMEASURED), (
        "a `use: threshold` corpus is missing from the frozen measurement without a reason; "
        f"add it to UNMEASURED or measure it: {sorted((declared - frozen) - set(UNMEASURED))}"
    )
    assert len(frozen) == 8, "the figures in metrics.md and measure_ceilings.py say eight"


def _weighted_by_rule() -> dict[str, list[float | None]]:
    """Each gated rule's LOC-weighted percentile, one per corpus, preferring the `named`
    population exactly as the prose does."""
    observed = json.loads(OBSERVATIONS.read_text())["observed"]
    found: dict[str, list[float | None]] = {}
    for rule, entry in observed.items():
        for populations in entry["corpora"].values():
            inner = populations.get("named") or populations["all_callables"]
            found.setdefault(rule, []).append(inner.get("ceiling_at_weighted_percentile"))
    return found


def test_every_population_carries_a_weighted_percentile() -> None:
    """**The figure `metrics.md` quotes has to come from the file, not from a `git log`.**

    Alves, Ypma & Visser weight each entity by its own lines, and that view is what the prose
    cites -- while `measure_ceilings.py` emitted only the unweighted `ceiling_at_percentile`
    until 2026-09-14. A number nobody can regenerate is how a five-corpus range survived the
    set growing to six.
    """
    for rule, values in _weighted_by_rule().items():
        assert values, rule
        for value in values:
            assert value is None or 0.0 <= value <= 100.0, (rule, value)


def test_the_cognitive_ceiling_leaves_the_band_on_javascript() -> None:
    """The correction the sixth corpus forced.

    "OXN's existing ceilings already sit on [Alves's p80/p90] boundaries" was true of the five
    corpora it was written against. Over all six the weighted range for cognitive 12 runs
    **P48.5 to P97.6**: eslint sits thirty points below the old floor, with more than half its
    lines in callables above the ceiling -- consistent with its 10.57% exceedance, the highest
    of the six by a factor of three.
    """
    cognitive = [value for value in _weighted_by_rule()["cognitive_complexity"] if value]

    assert min(cognitive) < 50, (
        "docs/metrics.md says the range starts at P48 on eslint; a floor above 50 means the "
        "corpus set or the weighting changed and that paragraph is stale"
    )
    assert max(cognitive) > 95, cognitive


def test_a_cache_that_lost_its_metrics_is_not_a_measurement(tmp_path: Path) -> None:
    """**The third form of the same failure, and the file count could not see it.**

    A cache can hold every file of today's tree and still have lost the metric rows for most of
    them -- `GraphStore.put_file` is a replace and takes them with it. Found on 2026-09-14 with
    `python-httpx` at 557 of 1,302 entities measured and 60 of 60 files cached: the guard passed,
    and a fresh measurement put cognitive complexity's population at 447 callables where the
    frozen record says 1,135. Freezing that would have republished a distribution over a third
    of the corpus as one over all of it.
    """
    import sqlite3

    from scripts.measure_ceilings import _measured

    db = tmp_path / "graph.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE entities (id TEXT)")
        connection.execute("CREATE TABLE metrics (entity_id TEXT, metric_key TEXT)")
        connection.executemany("INSERT INTO entities VALUES (?)", [(str(n),) for n in range(10)])
        connection.executemany(
            "INSERT INTO metrics VALUES (?, 'sloc')", [(str(n),) for n in range(4)]
        )

    assert _measured(db) == (4, 10), "measured entities, then entities -- the guard compares them"
