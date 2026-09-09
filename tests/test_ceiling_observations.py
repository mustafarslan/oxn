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
#: same filter measures the wrong thing, because the anonymous entities are where the methods
#: are: a Rust `impl` block is an unnamed class entity holding every method of its type, so
#: ripgrep reads 840 class entities against 395 named ones and its exceedance falls from 3.10%
#: to 0.25% under the filter. Go is starker -- all 379 of go-kit's structs are unnamed, so the
#: named row does not exist. The gate applies no name filter, so neither does this.
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


def test_the_known_false_positive_shape_is_small_and_recorded(audit: dict[str, int]) -> None:
    """A test class with many small test methods is a legitimate shape, and NOM rejects it.

    Seven of the rejections live in test files. That is the honest cost of the ceiling rather
    than a defect to hide: `oxn.yaml`'s `exclude` and advisory paths are how a project opts
    its tests out, and the number is pinned here so growth in it is visible.
    """
    assert audit["in_test_files"] <= audit["rejected"] // 10, (
        f"{audit['in_test_files']} of {audit['rejected']} rejections are in test files"
    )
