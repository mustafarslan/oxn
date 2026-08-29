"""Phase P1's exit criteria, asserted against real repositories.

These need the pinned corpora, so they skip when those are absent:

    python scripts/fetch_corpora.py --name python-httpx --name typescript-nest

Marked ``slow``; the default CI lane skips them. Thresholds are deliberately looser than
the numbers observed on a development machine (150k LOC: 1.4 s cold, 0.36 s warm, 100%
cache hit) because CI runners are slower and noisier. They exist to catch a regression in
kind -- an accidental full reparse, a cache key that stopped working -- not to benchmark.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

CORPORA = Path("benchmarks/corpora")
ROOTS = [CORPORA / "python-httpx", CORPORA / "typescript-nest"]

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not all(root.exists() for root in ROOTS),
        reason="corpora not fetched; see scripts/fetch_corpora.py",
    ),
]

#: A grammar that fails on more than this fraction of real files is not fit to gate on.
MAX_PARSE_ERROR_RATE = 0.01
COLD_BUDGET_S = 60.0
MIN_CACHE_HIT = 0.95


@pytest.fixture(scope="module")
def indexed(tmp_path_factory):
    from oxn.graph.indexer import Indexer

    cache = tmp_path_factory.mktemp("corpus") / "graph.db"
    with Indexer(root=CORPORA, cache_path=cache) as indexer:
        start = time.perf_counter()
        cold = indexer.index(ROOTS)
        cold_seconds = time.perf_counter() - start

        start = time.perf_counter()
        warm = indexer.index(ROOTS)
        warm_seconds = time.perf_counter() - start

        yield {
            "cold": cold,
            "warm": warm,
            "cold_seconds": cold_seconds,
            "warm_seconds": warm_seconds,
            "stats": indexer.store.stats(),
        }


def test_corpus_is_large_enough_to_be_meaningful(indexed) -> None:
    assert indexed["cold"].parsed > 1000


def test_cold_parse_is_within_budget(indexed) -> None:
    assert indexed["cold_seconds"] < COLD_BUDGET_S, (
        f"cold parse took {indexed['cold_seconds']:.1f}s for {indexed['cold'].parsed} files"
    )


def test_noop_rerun_is_served_entirely_from_cache(indexed) -> None:
    warm = indexed["warm"]
    assert warm.cache_hit_ratio >= MIN_CACHE_HIT, (
        f"only {warm.cache_hit_ratio:.1%} of files hit the cache; "
        f"{warm.parsed} were reparsed unnecessarily"
    )


def test_warm_run_is_much_faster_than_cold(indexed) -> None:
    """Incrementality is the point: a no-op must be dominated by hashing, not parsing."""
    assert indexed["warm_seconds"] < indexed["cold_seconds"] / 2


def test_parse_error_rate_is_negligible(indexed) -> None:
    cold = indexed["cold"]
    rate = len(cold.incomplete) / cold.considered
    assert rate < MAX_PARSE_ERROR_RATE, (
        f"{len(cold.incomplete)} of {cold.considered} files failed to parse cleanly "
        f"({rate:.2%}); e.g. {cold.incomplete[:3]}"
    )


def test_no_file_failed_to_read(indexed) -> None:
    assert indexed["cold"].failed == 0, indexed["cold"].errors


def test_graph_has_plausible_density(indexed) -> None:
    """A skeleton with far too few entities per file means the walk is missing definitions."""
    stats = indexed["stats"]
    per_file = stats["entities"] / stats["files"]
    assert 2.0 < per_file < 60.0, f"{per_file:.1f} entities per file looks wrong"
