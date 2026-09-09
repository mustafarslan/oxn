"""Why the risk profile is a score input and not an anti-gaming gate.

`docs/metrics.md` section 10.5 lists four mitigations against an agent gaming a ceiling. The
first -- "total complexity mass stays put while function count jumps" -- was falsified by
measurement on 2026-08-30 and is now reported, never gated. The third is *"gate on the risk
profile (% of LOC in high-risk buckets), not just on max values -- shredding moves LOC
between buckets rather than eliminating it"*, and section 10.5's own postscript said it was
"untouched by this and remains the right instrument at module scope".

It is not untouched. It fails the same control, harder, and this file is the measurement.

A risk profile is a statement about a *distribution*, and splitting a function is precisely
the operation that moves every one of its lines into a lower bucket -- the per-entity value
falls because the entity got smaller. Cyclomatic complexity does not rescue it: its *mass* is
conserved and in fact grows under extraction, since every new function carries a `+1` base,
but its profile collapses to `low` exactly as cognitive's does. Mass and profile move in
opposite directions and neither discriminates.

The consequence is not that the work is wasted. The risk profile is the aggregation
`docs/metrics.md` section 10.2 specifies for the *health rating*, where being a distribution
is the whole point, and the gate shape it supports is the **ratchet** -- this module's
high-risk share may not rise -- rather than an absolute ceiling. Shredding detection stays
where 2026-08-30 left it: the `shredding` rule, plus cohesion and the judge.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: LOC-weighted boundaries for Python, from `benchmarks/ceiling-observations.json`'s corpus
#: (httpx) as `scripts/measure_ceilings.py` computes them. Low/moderate/high/very-high, the
#: four SIG buckets of `docs/metrics.md` section 10.2.
BOUNDARIES = {"cognitive_complexity": (1, 4, 9), "cyclomatic_complexity": (2, 5, 8)}


def _bucket(value: float, boundaries: tuple[int, int, int]) -> int:
    low, moderate, high = boundaries
    if value <= low:
        return 0
    if value <= moderate:
        return 1
    return 2 if value <= high else 3


def _profile(source: str, metric_key: str) -> tuple[float, float]:
    """(% of LOC in high + very-high, total mass) for one module's callables.

    Measured straight through the engine rather than the cache: the profile is a pure
    function of the source, and going via `oxn metrics` would make this a test of where the
    store puts its database.
    """
    from oxn.graph.builder import build_file
    from oxn.languages import get_parser
    from oxn.metrics.engine import measure_file
    from oxn.profiles import get_profile

    profile = get_profile("python")
    data = source.encode()
    tree = get_parser("python").parse(data)
    parsed = build_file("m.py", data, profile, tree.root_node)
    measured = measure_file(list(parsed.entities), data, profile, tree.root_node)

    per_bucket = [0.0, 0.0, 0.0, 0.0]
    mass = 0.0
    for entity in measured:
        if entity.kind.value not in {"function", "method", "lambda"}:
            continue
        measurement = entity.values.get(metric_key)
        size = entity.values.get("sloc")
        if measurement is None or size is None:
            continue
        value, sloc = measurement.value, size.value
        mass += value
        per_bucket[_bucket(value, BOUNDARIES[metric_key])] += max(sloc, 1)
    total = sum(per_bucket) or 1.0
    return 100.0 * (per_bucket[2] + per_bucket[3]) / total, mass


@pytest.fixture(scope="module")
def variants() -> dict[str, str]:
    """The 2026-08-30 control: the same work, extracted well and extracted badly."""
    return {
        "cohesive": (FIXTURES / "cohesive_extraction.py.txt").read_text(),
        "shred": (FIXTURES / "shred_control.py.txt").read_text(),
    }


@pytest.mark.parametrize("metric_key", sorted(BOUNDARIES))
def test_the_shred_earns_a_better_risk_profile_than_the_good_refactoring(
    variants: dict[str, str], metric_key: str
) -> None:
    """The falsification, for both metrics: gating on the profile would invert the verdict.

    `dogfood.py`'s judge and the `shredding` rule both reject the shred and accept the
    cohesive extraction. A risk-profile gate does the opposite, and not marginally -- the
    shred reaches 0% high-risk LOC, a perfect profile, because sixteen trivial helpers put
    every line in the `low` bucket.
    """
    shred, _ = _profile(variants["shred"], metric_key)
    cohesive, _ = _profile(variants["cohesive"], metric_key)

    assert shred < cohesive, (
        f"{metric_key}: shred {shred:.1f}% vs cohesive {cohesive:.1f}% high-risk LOC -- if "
        "the shred no longer looks better, re-read docs/metrics.md section 10.5"
    )
    assert shred == pytest.approx(0.0), f"{metric_key}: the shred scored {shred:.1f}%, not 0%"


def test_cyclomatic_mass_and_profile_disagree_under_shredding(
    variants: dict[str, str],
) -> None:
    """Why picking the other metric does not save the instrument.

    Cognitive complexity loses its nesting increments under extraction, which is what made
    *mass* fall monotonically in 2026-08-30's table. Cyclomatic complexity has no nesting
    term, so its mass survives -- and grows, one `+1` per new function. That was the reason
    to think a cyclomatic profile might hold. It does not: the mass rises while the
    distribution collapses, so the two signals point in opposite directions at once.
    """
    shred_share, shred_mass = _profile(variants["shred"], "cyclomatic_complexity")
    cohesive_share, cohesive_mass = _profile(variants["cohesive"], "cyclomatic_complexity")

    assert shred_mass > cohesive_mass, "the shred's cyclomatic mass should be the larger"
    assert shred_share < cohesive_share, "while its high-risk share is the smaller"
