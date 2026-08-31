"""SCIP: a real index from the real indexer, and L0/L1 graded against it.

These are the numbers ADR-0002's gating policy rests on -- only a *certain* answer may
block a ceiling -- so they are re-measured rather than quoted."""

from __future__ import annotations

import os
import shutil

import pytest

from oracle_support import CORPUS

pytestmark = pytest.mark.oracle

SCIP_PYTHON = shutil.which("scip-python") or os.environ.get("OXN_SCIP_PYTHON")

requires_scip_python = pytest.mark.skipif(
    not SCIP_PYTHON,
    reason="scip-python not installed; npm install -g @sourcegraph/scip-python",
)


@requires_scip_python
@pytest.mark.slow
def test_wire_reader_round_trips_a_real_index(tmp_path) -> None:
    """The correctness proof for the hand-written protobuf reader.

    Synthetic bytes prove the decoding; only a real artifact proves the *field numbers*.
    Generating an index here means a SCIP schema change surfaces as a test failure rather
    than as silently missing edges.
    """
    from oxn.scip.index import load_index
    from oxn.scip.runner import Project, run_indexer

    project = tmp_path / "pkg"
    project.mkdir()
    (project / "__init__.py").write_text("")
    (project / "base.py").write_text(
        "class Repository:\n    def save(self, item):\n        return item\n"
    )
    (project / "service.py").write_text(
        "from pkg.base import Repository\n\n\n"
        "class SqlRepository(Repository):\n"
        "    def save(self, item):\n        return item\n"
    )

    output = run_indexer("python", tmp_path, tmp_path / "index.scip", Project(name="fixture"))
    index = load_index(output)

    paths = {document.relative_path for document in index.documents}
    assert {"pkg/base.py", "pkg/service.py"} <= paths

    service = index.by_path()["pkg/service.py"]
    assert any(occurrence.is_definition for occurrence in service.occurrences)
    assert any(
        relationship.is_implementation
        for symbol in service.symbols
        for relationship in symbol.relationships
    ), "inheritance should arrive as an is_implementation relationship"


@requires_scip_python
@pytest.mark.skipif(not CORPUS.exists(), reason="corpora not fetched")
@pytest.mark.slow
def test_scip_ingest_covers_a_real_corpus(tmp_path) -> None:
    """Phase exit criterion, stated as coverage rather than an undefined 'precision'.

    Measured on httpx: index built in ~4s, 99% of declarations matched a symbol, 96% of
    call sites resolved to a target. Thresholds sit below the observed values because CI
    runners and indexer versions vary.
    """
    from oxn.graph.indexer import Indexer
    from oxn.scip.ingest import ingest_index
    from oxn.scip.runner import Project, run_indexer

    corpus = CORPUS.resolve()
    output = run_indexer(
        "python", corpus, tmp_path / "httpx.scip", Project(name="httpx", version="0.28")
    )

    with Indexer(root=corpus, cache_path=tmp_path / "graph.db") as indexer:
        report = ingest_index(indexer, output)

    assert report.matched_documents > 40
    assert report.definition_coverage >= 0.90, f"{report.definition_coverage:.1%}"
    assert report.call_coverage >= 0.85, f"{report.call_coverage:.1%}"
    assert report.seconds < 60
    assert report.edges > 1000


# ---- L0/L1 measured against L2 --------------------------------------------------------------


@requires_scip_python
@pytest.mark.skipif(not CORPUS.exists(), reason="corpora not fetched")
@pytest.mark.slow
def test_l0_l1_accuracy_against_scip_ground_truth(tmp_path) -> None:
    """The published L0/L1 accuracy table, re-measured.

    Measured on httpx: 99.8% precision at 65.6% recall when L1 is *certain*, 70.3% precision
    at 100% recall when it also guesses. That split is the gating policy in ADR-0002 -- only
    certain answers may block.

    30.5% of call sites are excluded because scip-python 0.6.6 contradicts the source it
    indexed, naming an alphabetically adjacent symbol for names re-exported through a
    package `__init__.py`. Grading against a ground truth wrong one time in five would
    measure the oracle rather than us.
    """
    from oxn.resolve.measure import measure_corpus
    from oxn.scip.runner import Project, run_indexer

    corpus = CORPUS.resolve()
    index = run_indexer(
        "python", corpus, tmp_path / "httpx.scip", Project(name="httpx", version="0.28")
    )
    accuracy = measure_corpus(corpus, index)

    assert accuracy.graded_call_sites > 500, "corpus too small to mean anything"
    assert accuracy.confident_precision >= 0.95, (
        f"confident precision {accuracy.confident_precision:.1%}; "
        f"first disagreements: {accuracy.disagreements[:3]}"
    )
    assert accuracy.confident_recall >= 0.50, f"confident recall {accuracy.confident_recall:.1%}"
    assert accuracy.precision >= 0.60, f"overall precision {accuracy.precision:.1%}"
    # The oracle's own error rate is worth watching: a jump means scip-python changed.
    assert accuracy.excluded_share <= 0.45, f"{accuracy.excluded_share:.1%} excluded"
