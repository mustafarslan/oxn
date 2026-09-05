"""Retrieval quality on ADR corpora that are not OXN's.

ADR-0006 section 5: precision@3 over OXN's own six decisions is not evidence, so the number
that counts is measured here -- five pinned repositories, 100 pairs, each ranked **only
against its own repository's decisions**, which is what `get_architectural_context` will
actually do. Pooling is for the statistics, never for the ranking; nothing is ever ranked
against a foreign decision.

The labels are citations: a commit that cited an ADR while doing the work it governs, with
the citation stripped from the query. `scripts/extract_citation_labels.py` explains the
four filters and why each throws away real pairs.

Two slices are reported alongside the headline, because two properties of the corpus would
otherwise be invisible: half of every usable pair in open source is agent-written, and some
task texts repeat the cited decision's title. Neither is dropped silently.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

from oxn.context.bm25 import BM25
from oxn.context.decisions import decision_documents
from oxn.rules.adr import load_decisions

ROOT = Path(__file__).resolve().parent.parent
LABELS = ROOT / "benchmarks" / "retrieval-labels-external.json"
CORPORA = ROOT / "benchmarks" / "corpora"

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not LABELS.exists()
        or not all(
            (CORPORA / name / ".git").exists() for name in json.loads(LABELS.read_text())["corpora"]
        ),
        reason="retrieval corpora not fetched; see scripts/fetch_corpora.py --use retrieval",
    ),
]

#: Measured 2026-09-05 on 100 pairs over five repositories. Update in the same commit as
#: whatever moved them -- a ranker change, or a re-extraction of the labels.
RECORDED = {
    "all": (0.610, 0.820, 0.726),
    "human-authored": (0.604, 0.833, 0.722),
    "no title restated": (0.545, 0.773, 0.680),
    "strict": (0.481, 0.741, 0.632),
    "other repositories": (0.583, 0.875, 0.723),
}


def _extractor():
    """The one definition of what a query is. Duplicating it here is how the test and the
    extractor come to disagree about what was measured."""
    spec = importlib.util.spec_from_file_location(
        "citation_labels", ROOT / "scripts" / "extract_citation_labels.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["citation_labels"] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Corpus:
    """One repository's decisions, indexed. A pair is only ever ranked against its own."""

    name: str
    root: Path
    index: BM25
    #: Decision identifier -> the number a citation names.
    number: dict[str, int]
    #: Decisions ordered by how often this repository cites them: the baseline to beat.
    prior: list[int]


def _corpus(name: str, spec: dict) -> Corpus:
    root = CORPORA / name
    decisions = load_decisions(root, Path(spec["adr_dir"]))
    frequency = Counter(gold for pair in spec["pairs"] for gold in pair["gold"])
    return Corpus(
        name=name,
        root=root,
        index=BM25(decision_documents(decisions)),
        number={d.identifier: int(re.sub(r"\D", "", d.identifier) or -1) for d in decisions},
        prior=[identifier for identifier, _ in frequency.most_common()],
    )


@pytest.fixture(scope="module")
def ranked() -> list[dict[str, object]]:
    """Every pair, ranked against its own repository's decisions."""
    extractor = _extractor()
    labels = json.loads(LABELS.read_text())
    return [
        _rank(extractor, corpus, pair)
        for name, spec in labels["corpora"].items()
        for corpus in [_corpus(name, spec)]
        for pair in spec["pairs"]
    ]


def _rank(extractor, corpus: Corpus, pair: dict) -> dict[str, object]:
    fields = extractor.git(corpus.root, "log", "-1", "--format=%s\x1f%b", pair["sha"]).split("\x1f")
    query = extractor.task_text(fields[0].strip(), fields[1] if len(fields) > 1 else "")
    return {
        "corpus": corpus.name,
        "gold": set(pair["gold"]),
        #: The decisions that actually parsed from the pinned clone -- the set a label has
        #: to be inside. Deliberately not `prior`, which is built *from* the labels and so
        #: contains every gold id by construction.
        "known": set(corpus.number.values()),
        "order": [corpus.number[hit.identifier] for hit in corpus.index.rank(query)],
        "prior": corpus.prior,
        "agent": pair["agent_authored"],
        "restated": pair["restates_title"],
    }


def _score(rows: list[dict], key: str = "order") -> tuple[float, float, float]:
    """Precision@1, precision@3 and MRR, rounded to where 100 pairs can support them."""

    def hit(row, k):
        return any(name in row["gold"] for name in row[key][:k])

    n = len(rows)
    reciprocal = sum(
        next((1 / rank for rank, x in enumerate(row[key], 1) if x in row["gold"]), 0.0)
        for row in rows
    )
    return (
        round(sum(hit(row, 1) for row in rows) / n, 3),
        round(sum(hit(row, 3) for row in rows) / n, 3),
        round(reciprocal / n, 3),
    )


def _slices(rows: list[dict]) -> dict[str, list[dict]]:
    return {
        "all": rows,
        "human-authored": [r for r in rows if not r["agent"]],
        "no title restated": [r for r in rows if not r["restated"]],
        "strict": [r for r in rows if not r["agent"] and not r["restated"]],
        "other repositories": [r for r in rows if r["corpus"] != "adr-agentic-dev-team"],
    }


def test_the_recorded_external_numbers_reproduce(ranked) -> None:
    measured = {name: _score(rows) for name, rows in _slices(ranked).items()}
    assert measured == RECORDED, (
        f"measured {measured}; recorded {RECORDED}. If the ranker changed or the labels "
        "were re-extracted, update the record in the same commit."
    )


@pytest.mark.parametrize("slice_name", list(RECORDED))
def test_bm25_beats_the_citation_prior(ranked, slice_name: str) -> None:
    """The claim OXN's own corpus could not support (ADR-0006 section 5a), and the reason
    the external corpus was worth the search.

    The baseline is the same shape as the one that beat BM25 there: rank every decision by
    how often it is cited, ignore the task entirely. Beating it means the words are doing
    work. It must hold in every slice, including the strict one -- human-authored pairs
    whose text does not repeat the cited decision's title.
    """
    rows = _slices(ranked)[slice_name]
    _, bm25_at_3, _ = _score(rows)
    _, prior_at_3, _ = _score(rows, key="prior")
    assert bm25_at_3 > prior_at_3, (
        f"{slice_name} (n={len(rows)}): BM25 P@3 {bm25_at_3} against a citation-frequency "
        f"prior of {prior_at_3}"
    )


def test_the_strict_slice_is_large_enough_to_mean_anything(ranked) -> None:
    """Guarding the guard, as `test_rules_corpus` does: a precision over five pairs is a
    number, not a measurement."""
    assert len(_slices(ranked)["strict"]) >= 25


def test_every_label_points_at_a_decision_that_still_exists(ranked) -> None:
    """A gold id naming a renumbered or deleted ADR would score as a miss forever.

    Checked against the decisions that *parsed from the pinned clone*. An earlier version
    compared against `prior`, which is built from the labels themselves and therefore
    contains every gold id whatever the corpus says -- a test that could not fail, which is
    the third one this project has caught by asking what would happen if it were wrong.
    """
    for row in ranked:
        assert row["gold"] <= row["known"], (
            f"{row['corpus']}: labels {sorted(row['gold'] - row['known'])} name decisions "
            "that do not exist at the pinned commit"
        )
