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


# ---- the same table, for Go ----------------------------------------------------------------

GO_CORPUS = CORPUS.parent / "go-kit"

requires_scip_go = pytest.mark.skipif(
    not (shutil.which("scip-go") or os.environ.get("OXN_SCIP_GO")),
    reason="scip-go not installed; go install github.com/scip-code/scip-go/cmd/scip-go@latest",
)


@requires_scip_go
@pytest.mark.skipif(not GO_CORPUS.exists(), reason="corpora not fetched")
@pytest.mark.slow
def test_go_l2_covers_the_corpus(tmp_path) -> None:
    """P5's coverage criterion, for the second language to have an L2 at all.

    Measured on go-kit (243 documents): 100% of declarations matched a symbol and 88.3% of
    call sites resolved, ingest in 0.4 s.

    **Indexing is one second; the prerequisite is not.** `scip-go` runs `go/packages`, so
    the module must already be buildable -- on go-kit, whose closure includes the AWS SDK
    and gRPC, the first `go build ./...` took **9 minutes**, at 4% CPU, because it is a
    download rather than a compile. P5's "<60 s" budget is about indexing and is met with
    room to spare; the fetch is a one-time cost that belongs to the toolchain, not to OXN,
    and is worth stating so nobody reads 1 s as the cold number.
    """
    from oxn.graph.indexer import Indexer
    from oxn.scip.ingest import ingest_index
    from oxn.scip.runner import Project, run_indexer

    corpus = GO_CORPUS.resolve()
    index = run_indexer("go", corpus, tmp_path / "go-kit.scip", Project("go-kit", "0.0.0"))

    with Indexer(root=corpus, cache_path=tmp_path / "graph.db") as indexer:
        report = ingest_index(indexer, index)

    assert report.matched_documents > 200
    assert report.definition_coverage >= 0.90, f"{report.definition_coverage:.1%}"
    assert report.call_coverage >= 0.85, f"{report.call_coverage:.1%}"
    assert report.seconds < 60


@requires_scip_go
@pytest.mark.skipif(not GO_CORPUS.exists(), reason="corpora not fetched")
@pytest.mark.slow
def test_go_l0_l1_accuracy_is_published_and_is_worse_than_python(tmp_path) -> None:
    """The per-language half of P5's exit criterion, and the reason it is worth publishing.

    Go on go-kit: **88.3% precision when L1 is certain**, against Python's 99.8% on httpx.
    Overall 59.3% at 100% recall. Nothing was excluded -- scip-go did not contradict its own
    source anywhere, where scip-python does at 30.5% of sites.

    That gap is a finding, not noise. **ADR-0002's gating policy -- only a certain answer may
    block -- was calibrated on the one language that had been measured.** A Go project gated
    on L1-certain call resolution is gated on a signal wrong about one time in nine, so Go
    Tier-3 metrics are not yet gate-quality and the floor here is set where the language
    actually is rather than where Python is.

    The remaining error is receiver dispatch: `go-kit` declares four types in one file each
    implementing `With`, and L1 has no receiver types to tell them apart. Fixing the *false
    confidence* took this from 83.9% to 88.3% (see `test_resolve.py`); closing the rest needs
    type information, which is what L2 is for.
    """
    from oxn.resolve.measure import measure_corpus
    from oxn.scip.runner import Project, run_indexer

    corpus = GO_CORPUS.resolve()
    index = run_indexer("go", corpus, tmp_path / "go-kit.scip", Project("go-kit", "0.0.0"))
    accuracy = measure_corpus(corpus, index, language="go")

    assert accuracy.graded_call_sites > 1000, "corpus too small to mean anything"
    assert accuracy.confident_precision >= 0.85, (
        f"confident precision {accuracy.confident_precision:.1%}; "
        f"first disagreements: {accuracy.disagreements[:3]}"
    )
    assert accuracy.recall >= 0.95, f"recall {accuracy.recall:.1%}"
    # Python's number, asserted here as a *ceiling* on the Go claim: if Go ever reaches it,
    # this test should fail and the gating policy be revisited on the evidence.
    assert accuracy.confident_precision < 0.99, (
        "Go confident precision has reached Python's level; re-examine ADR-0002's gating "
        "policy, which currently treats L1 certainty as language-independent"
    )


# ---- and for Rust ---------------------------------------------------------------------------

RUST_CORPUS = CORPUS.parent / "rust-ripgrep"

requires_rust_analyzer = pytest.mark.skipif(
    not (shutil.which("rust-analyzer") and shutil.which("cargo")),
    reason="rust-analyzer and a Rust toolchain are both required; `scip` loads the "
    "workspace through cargo and panics without it",
)


@requires_rust_analyzer
@pytest.mark.skipif(not RUST_CORPUS.exists(), reason="corpora not fetched")
@pytest.mark.slow
def test_rust_l2_and_l0_l1_accuracy(tmp_path) -> None:
    """The third language in the table, and the one that tells you what the Go gap was.

    On ripgrep: L2 coverage 99.2% of declarations and 99.2% of call sites, index built in
    40 s against the 60 s budget. L0/L1 scores **99.1% precision when certain** (90.8%
    overall at 100% recall) -- Python's number, not Go's.

    That is the finding. Rust is not more resolvable than Go in general; its *methods live
    inside `impl` blocks*, so they are not file-level declarations and never collide in the
    bare-name table the way four `With` methods do in one Go file. The shape that matters is
    whether a method is a top-level declaration, and Go is the only launch language where it
    is. ADR-0002's gating policy holds for Python, TypeScript and Rust, and Go is the
    exception rather than the rule -- which one language could not have told us and two
    still could not.

    **73.7% of call sites are excluded**, far above scip-python's 30.5%: rust-analyzer's
    symbol for a site often does not spell what the source spells, most visibly around
    generics and trait dispatch. The graded quarter is what remains after refusing to let
    the oracle arbitrate names it disagrees with the source about, and 1,747 sites is still
    a corpus.
    """
    from oxn.graph.indexer import Indexer
    from oxn.resolve.measure import measure_corpus
    from oxn.scip.ingest import ingest_index
    from oxn.scip.runner import run_indexer

    corpus = RUST_CORPUS.resolve()
    index = run_indexer("rust", corpus, tmp_path / "ripgrep.scip", timeout=600)

    with Indexer(root=corpus, cache_path=tmp_path / "graph.db") as indexer:
        report = ingest_index(indexer, index)
    assert report.definition_coverage >= 0.90, f"{report.definition_coverage:.1%}"
    assert report.call_coverage >= 0.85, f"{report.call_coverage:.1%}"

    accuracy = measure_corpus(corpus, index, language="rust")
    assert accuracy.graded_call_sites > 1000, "corpus too small to mean anything"
    assert accuracy.confident_precision >= 0.95, (
        f"confident precision {accuracy.confident_precision:.1%}; "
        f"first disagreements: {accuracy.disagreements[:3]}"
    )
