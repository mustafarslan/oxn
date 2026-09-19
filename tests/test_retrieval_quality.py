"""How good the ranking is, on labels nobody hand-wrote.

ADR-0006 section 5. The pairs are frozen in `benchmarks/retrieval-labels-oxn.json` by
`scripts/extract_retrieval_labels.py`: task = a commit subject, gold = the decisions whose
`applies-to` covered a file that commit changed. Freezing them is what makes a number
assertable -- every new commit would otherwise move the label set underneath the test.

**The corpus is frozen too, and for seventeen commits it was not.** The label script argues
that "a test cannot pin a number that moves underneath it" and freezes the queries. The
*documents* were left live -- this repository's own ADRs, which nearly every commit edits --
so half the experiment was frozen and half was not, and that asymmetry was the defect.
`benchmarks/retrieval-corpus-oxn.json` is now the other half, written by
`scripts/freeze_retrieval_corpus.py` and stamped with the commit it describes.

It cost seventeen re-pins in three days, P@1 0.377 -> 0.623, without one line of the ranker
changing: ADR-0002 is gold for most of the label set and roughly tripled in length over those
commits. The number looked like retrieval improving and was the gold document getting longer.
The worse half is that a test failing on every documentation edit teaches you to re-pin
without reading it, so a real ranker regression would have arrived looking exactly like the
seventeen benign ones.

**What this file deliberately does not assert.** Not that BM25 beats the constant baseline,
because on this corpus it does not (section 5a); and not that 0.623 is a quality floor,
because at five documents and 53 pairs it is noise. The quality floor belongs on the
external corpus that section 5 requires. What is asserted here is *reproducibility* -- the
recorded numbers are what this ranker produces on that frozen corpus -- so that a change to
the ranker carries its delta in the diff, exactly as `.oxn/baseline.json` does for the gate,
and a change to an ADR carries none.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from oxn.context.bm25 import BM25, Document
from oxn.context.decisions import decision_documents
from oxn.rules.adr import load_decisions

ROOT = Path(__file__).resolve().parent.parent
LABELS = ROOT / "benchmarks" / "retrieval-labels-oxn.json"
CORPUS = ROOT / "benchmarks" / "retrieval-corpus-oxn.json"

#: Measured on 53 pairs over five decisions, against the corpus frozen at `bca8801`.
#:
#: **This list used to be seventeen entries long and is closed.** Every one of them was an
#: ADR edit rather than a ranker change -- 0.377/0.579 at the start, 0.623/0.760 at the end,
#: with ADR-0002 tripling in length underneath a fixed query set. The history is kept in
#: `scripts/freeze_retrieval_corpus.py`, which is now the only thing that can move these two
#: numbers without a ranker change, and moving them there is a deliberate act rather than a
#: step in getting a build green.
#:
#: A number that lives in a script drifts silently; one that lives here carries its delta.
#: That was always the right principle and it was being applied to a moving corpus, which
#: made the delta noise. It measures the ranker now.
RECORDED_P_AT_1 = 0.623
RECORDED_MRR = 0.760


@pytest.fixture(scope="module")
def labels() -> dict[str, object]:
    return json.loads(LABELS.read_text())


@pytest.fixture(scope="module")
def pairs(labels) -> list[tuple[str, set[str]]]:
    return [(pair["task"], set(pair["gold"])) for pair in labels["pairs"]]


@pytest.fixture(scope="module")
def ranked() -> BM25:
    """The ranker over the **frozen** corpus, which is what the pinned numbers describe."""
    frozen = json.loads(CORPUS.read_text())["documents"]
    return BM25([Document(identifier, text) for identifier, text in sorted(frozen.items())])


@pytest.fixture(scope="module")
def ranked_live(labels) -> BM25:
    """The ranker over the ADRs as they are right now.

    The floor tests read this one on purpose: they are the check that OXN's MCP server still
    works on OXN's own current decisions, and they assert *inequalities*, so an ADR edit
    cannot make them fail spuriously the way an exact pin could.
    """
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


def test_the_ranker_is_at_least_not_worse_than_chance(ranked_live, labels, pairs) -> None:
    """The weakest claim worth making on a corpus this size, and it is nearly all of what
    the corpus can support -- see ADR-0006 section 5a for why the stronger claim is false."""
    measured, _ = _measure(lambda task: [hit.identifier for hit in ranked_live.rank(task)], pairs)
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
    """ADR-0006's "bundle size stays under budget on all corpus tasks", asserted
    against the 53 real tasks rather than a handful invented for the occasion.

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


def test_the_calibration_sweep_still_says_what_it_records(ranked, pairs) -> None:
    """`BM25_K1` and `BM25_B` cite a sweep over this corpus; this is that sweep.

    Only the half that runs without `git` and the external clones. The other hundred pairs are
    pinned by `test_retrieval_external`'s recorded numbers at the shipped values, and the
    *direction* there -- P@1 rising with b where it falls here -- is what makes fitting b a
    statement about a corpus rather than about retrieval.
    """
    from oxn import thresholds

    frozen = json.loads(CORPUS.read_text())["documents"]
    documents = [Document(name, text) for name, text in sorted(frozen.items())]

    def at(k1: float, b: float) -> float:
        index = BM25(documents, k1=k1, b=b)
        return _measure(lambda task: [hit.identifier for hit in index.rank(task)], pairs)[0]

    # k1 is flat across Robertson & Zaragoza's band -- one pair either way -- so fitting it
    # here would be chasing noise, which is what its `fit_when` now says.
    band = {at(k1, thresholds.BM25_B) for k1 in (1.2, 1.5, 1.8, 2.0)}
    assert max(band) - min(band) <= 0.02, band

    # b is not flat, and on *this* corpus less normalisation scores better -- the opposite of
    # the external set. Five decisions is small enough that b=0 merely reproduces the breadth
    # prior this file already declines to claim BM25 beats.
    assert at(thresholds.BM25_K1, 0.0) > at(thresholds.BM25_K1, 1.0)


def test_both_bm25_parameters_count_the_pairs_they_were_swept_over(labels) -> None:
    """`oxn calibration` claims 153 observations each; this is where 153 comes from.

    53 pairs from this repository's history and 100 from five other repositories. Pinned
    because the count is the part that rots: labels get re-extracted -- seventeen times in
    three days, once -- and a provenance still quoting the old n reads as authoritative.
    """
    external = json.loads((ROOT / "benchmarks" / "retrieval-labels-external.json").read_text())
    pairs = len(labels["pairs"]) + sum(
        len(corpus["pairs"]) for corpus in external["corpora"].values()
    )

    from oxn.calibration import parameters

    swept = [p for p in parameters() if p.name in {"BM25_K1", "BM25_B"}]
    assert len(swept) == 2
    for parameter in swept:
        assert parameter.observations == pairs, parameter.name
        assert parameter.is_provisional


def test_the_bundle_cap_counts_the_tasks_it_was_priced_over(pairs) -> None:
    """`MAX_BUNDLE_CONSTRAINTS` was the last parameter at zero observations, and it was not
    blocked by the experiment its `fit_when` named.

    `observations` counts a parameter's cost at its current value, not what it was fitted to,
    and a cost needs no experiment -- the stalled agent-arms run blocked the fit and nothing else.
    Priced over these 53 tasks the cap binds on **every one**, hiding a median 9 of this
    project's 16 constraints.

    Pinned against the label file rather than restated, so a change to the labels moves the
    two together or fails here.
    """
    from oxn.calibration import parameters

    cap = next(p for p in parameters() if p.name == "MAX_BUNDLE_CONSTRAINTS")
    assert cap.observations == len(pairs), (
        f"the cap claims {cap.observations} observations against {len(pairs)} labelled tasks"
    )
    assert cap.is_provisional, "priced is not fitted; this stays a judgement"
