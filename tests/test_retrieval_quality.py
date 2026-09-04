"""How good the ranking is, on labels nobody hand-wrote.

ADR-0006 section 5. The pairs are frozen in `benchmarks/retrieval-labels-oxn.json` by
`scripts/extract_retrieval_labels.py`: task = a commit subject, gold = the decisions whose
`applies-to` covered a file that commit changed. Freezing them is what makes a number
assertable -- every new commit would otherwise move the label set underneath the test.

**What this file deliberately does not assert.** Not that BM25 beats the constant baseline,
because on this corpus it does not (section 5a); and not that 0.377 is a quality floor,
because at five documents and 53 pairs it is noise. The quality floor belongs on the
external corpus that section 5 requires. What is asserted here is *reproducibility* -- the
recorded numbers are what the current ranker and the current decisions produce -- so that
any change to either carries its delta in the diff, exactly as `.oxn/baseline.json` does
for the gate.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from oxn.context.bm25 import BM25
from oxn.context.decisions import decision_documents
from oxn.rules.adr import load_decisions

ROOT = Path(__file__).resolve().parent.parent
LABELS = ROOT / "benchmarks" / "retrieval-labels-oxn.json"

#: Measured 2026-09-04 on 53 pairs over five decisions. Update these in the same commit as
#: whatever moved them -- a ranker change, or an edit to any ADR body, since the corpus
#: being ranked is the repository's own decisions.
RECORDED_P_AT_1 = 0.377
RECORDED_MRR = 0.581


@pytest.fixture(scope="module")
def labels() -> dict[str, object]:
    return json.loads(LABELS.read_text())


@pytest.fixture(scope="module")
def pairs(labels) -> list[tuple[str, set[str]]]:
    return [(pair["task"], set(pair["gold"])) for pair in labels["pairs"]]


@pytest.fixture(scope="module")
def ranked(labels) -> BM25:
    wanted = set(labels["decisions"])
    return BM25(decision_documents(d for d in load_decisions(ROOT) if d.identifier in wanted))


def _measure(order_of, pairs) -> tuple[float, float]:
    """Precision@1 and mean reciprocal rank, rounded to where the corpus can support them."""
    first = reciprocal = 0.0
    for task, gold in pairs:
        order = order_of(task)
        first += 1.0 if order and order[0] in gold else 0.0
        rank = next((position for position, name in enumerate(order, 1) if name in gold), None)
        reciprocal += 1 / rank if rank else 0.0
    return round(first / len(pairs), 3), round(reciprocal / len(pairs), 3)


def _baselines(labels, pairs) -> dict[str, float]:
    """The three rankings any retrieval must be read against, since none of them reads text."""
    breadth = [name for name, _ in Counter(n for _, gold in pairs for n in gold).most_common()]
    expected_gold = sum(len(gold) for _, gold in pairs) / len(pairs)
    return {
        "random P@1": round(expected_gold / len(labels["decisions"]), 3),
        "breadth-prior P@1": _measure(lambda _: breadth, pairs)[0],
        "breadth-prior MRR": _measure(lambda _: breadth, pairs)[1],
    }


def test_the_recorded_retrieval_numbers_reproduce(ranked, labels, pairs) -> None:
    """A number that lives in a script drifts silently; one that lives here carries its delta."""
    measured = _measure(lambda task: [hit.identifier for hit in ranked.rank(task)], pairs)
    assert measured == (RECORDED_P_AT_1, RECORDED_MRR), (
        f"P@1 {measured[0]} MRR {measured[1]} against a record of "
        f"{RECORDED_P_AT_1} / {RECORDED_MRR}, on {len(pairs)} pairs over "
        f"{len(labels['decisions'])} decisions. Baselines: {_baselines(labels, pairs)}. "
        "If a ranker or an ADR body changed, update the record in the same commit."
    )


def test_the_ranker_is_at_least_not_worse_than_chance(ranked, labels, pairs) -> None:
    """The weakest claim worth making on a corpus this size, and it is nearly all of what
    the corpus can support -- see ADR-0006 section 5a for why the stronger claim is false."""
    measured, _ = _measure(lambda task: [hit.identifier for hit in ranked.rank(task)], pairs)
    assert measured >= _baselines(labels, pairs)["random P@1"]


def test_every_labelled_decision_still_exists(labels) -> None:
    """A gold id naming a deleted or renamed ADR would rot in silence and score as a miss."""
    present = {decision.identifier for decision in load_decisions(ROOT)}
    assert set(labels["decisions"]) <= present


def test_no_label_names_a_decision_that_governs_everything(labels) -> None:
    """ADR-0001 is scoped `**`, so it is gold for every commit. A label set with an
    always-right answer measures nothing, and the extractor drops such decisions."""
    scoped = {d.identifier: d.applies_to for d in load_decisions(ROOT)}
    assert all("**" not in scoped[name] for name in labels["decisions"])


# ---- the other half of P8's exit criterion: the bundle stays inside its budget -----------


def test_the_bundle_stays_under_budget_on_every_labelled_task(pairs) -> None:
    """"Bundle size stays under budget on all corpus tasks" (ROADMAP P8), asserted against
    the 53 real tasks rather than against a handful invented for the occasion.

    The cap is trivially satisfiable by a `[:limit]`, so what this actually guards is the
    accounting around it: `omitted` must always say how many were hidden, because a budget
    that silently drops constraints is indistinguishable from a gate that stopped enforcing
    them.
    """
    from oxn import thresholds
    from oxn.config import Config
    from oxn.context.bundle import Project, build_bundle
    from oxn.graph.sources import iter_source_files

    config = Config.load(ROOT)
    found = iter_source_files([ROOT], base=ROOT, exclude=config.exclude)
    project = Project(
        config=config,
        decisions=tuple(load_decisions(ROOT)),
        paths=tuple(str(path.relative_to(ROOT)) for path in found),
    )
    total = len(build_bundle(project, limit=10_000).constraints)
    for task, _ in pairs:
        bundle = build_bundle(project, task=task)
        assert len(bundle.constraints) <= thresholds.MAX_BUNDLE_CONSTRAINTS
        assert len(bundle.constraints) + bundle.omitted == total
