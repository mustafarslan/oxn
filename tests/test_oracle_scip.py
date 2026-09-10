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

    Go on go-kit: **99.7% precision when L1 is certain** at 64.1% confident recall, 71.4%
    overall at 100% recall, against Python's 100% on httpx. Nothing is excluded -- scip-go
    does not contradict its own source anywhere, where scip-python does at 30.5% of sites.

    **Confident recall was 55.5% and overall precision 59.3% until Go imports resolved.**
    `resolve_call`'s second step -- "a declaration in a file this one imports" -- had never
    once fired for Go, because `github.com/go-kit/kit/metrics` resolved to nothing and
    go-kit's dependency graph held *zero* in-tree edges (`tests/test_go_resolution.py`). It
    added 355 confident answers, 302 of them right.

    **Confident precision fell 0.9 points in the process, and that is the honest trade.**
    Correct confident answers went 773 -> 1,075; wrong ones 102 -> 155. All 155 are
    `selector_expression` -- receiver dispatch -- and they split 102 same-file (the `With`
    collisions this test has always described) and 53 new ones of the *other* shape:
    `c.Value()` where `c` is a local of an imported type, answered with the imported
    package's `Value`. Those 53 are what the import table exists to stop, and stopping them
    needs `resolve_call` to see the qualifier -- the next commit, not this one.

    That gap remains a finding, not noise. **ADR-0002's gating policy -- only a certain
    answer may block -- was calibrated on the one language that had been measured.** A Go
    project gated on L1-certain call resolution is gated on a signal wrong about one time in
    eight, so Go Tier-3 metrics are not gate-quality and the floor here is set where the
    language is rather than where Python is.
    """
    from oxn.resolve.measure import measure_corpus
    from oxn.scip.runner import Project, run_indexer

    corpus = GO_CORPUS.resolve()
    index = run_indexer("go", corpus, tmp_path / "go-kit.scip", Project("go-kit", "0.0.0"))
    accuracy = measure_corpus(corpus, index, language="go")

    assert accuracy.graded_call_sites > 1000, "corpus too small to mean anything"
    assert accuracy.confident_precision >= 0.95, (
        f"confident precision {accuracy.confident_precision:.1%}; "
        f"first disagreements: {accuracy.disagreements[:3]}"
    )
    assert accuracy.recall >= 0.95, f"recall {accuracy.recall:.1%}"
    # The guard Rust did not have, and Go's real value is zero.
    assert accuracy.excluded_share <= 0.05, f"{accuracy.excluded_share:.1%} excluded"
    # **This assertion has fired and been retired.** It read `< 0.99` and said: if Go ever
    # reaches Python's level, revisit ADR-0002's gating policy on the evidence. Go reached
    # 99.7% once receiver calls stopped taking the import step, so the premise it was
    # guarding -- that L1 certainty is worth less in Go than in Python -- is no longer what
    # the corpora say. What is left of the old caution is the *recall* gap: Go is certain
    # about 64.1% of its call sites where Python is certain about 65.5% and Java 88.0%.
    assert accuracy.confident_recall < 0.90, (
        "Go is now as *often* certain as the languages whose methods live in classes; "
        "re-examine what `_declared_in` treats as top-level"
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
    """The third language in the table, and the one that showed the *grader* was wrong.

    On ripgrep: L2 coverage 99.2% of declarations and 99.2% of call sites, index built in
    40 s against the 60 s budget. L0/L1 scores **99.2% precision when certain** at 42.3%
    confident recall; 53.0% overall at 100% recall, over 6,642 graded call sites.

    **Those numbers replace the ones this test asserted when it was written, and the
    difference is a defect in `symbol_tail`, not a change to the resolver.** It published
    99.1% confident precision on 1,747 sites with 73.7% excluded, and attributed the
    exclusions to generics and trait dispatch -- inferred, never inspected. In fact 4,844 of
    the 4,905 were correct oracle answers the grader threw away because it could not parse a
    `rust-analyzer` symbol: SCIP's five space-separated fields were never stripped, so a
    crate-level function came back as its whole symbol, and the `[Impl]` prefix on a method
    was read as part of the name. Rust was graded on the quarter of its call sites that
    happened to survive, and that quarter was not representative.

    **What is left after the fix is that Rust looks like Go, not like Python.** The claim
    this test made -- that `impl` blocks keep Rust's methods out of the bare-name table where
    Go's four `With` methods collide -- is **retracted**. `resolve_call(path, name)` takes a
    bare name and never looks at the impl, so `new`, declared in nine `impl` blocks in
    `line_buffer.rs` alone, collides exactly as Go's methods do; that one name is most of the
    gap between 51.2% overall and 90.9% confident.

    **The confident errors were one shape, and it is now closed.** Of 277 confident-wrong
    sites, 265 were `field_expression` -- receiver dispatch, `reader.consume_all()` -- and
    every one of them came from the calling file's own top-level declarations answering a
    call on a receiver, which scored **0 for 265**. `resolve_call` no longer takes that step
    for a qualified call, and confident precision went 90.9% -> 99.6% for 4 points of
    confident recall. The 11 `scoped_identifier` and 1 `generic_function` remain.

    Why Python reaches 99.8% is **not** established by this. It may be a property of the
    language or of httpx's naming; nothing here measures which, and the last causal story
    told from this corpus turned out to be an artifact.

    Exclusions are now 0.15%: ten `use ... as ...` aliases, where the source writes
    `generate_version_pcre2` and SCIP names the definition `generate_pcre2`. Those are
    correct exclusions, and they are the *same* import-alias gap the inverted tests in
    `test_resolve.py` record for Go.
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
    assert accuracy.graded_call_sites > 5000, "corpus too small to mean anything"
    assert accuracy.confident_precision >= 0.95, (
        f"confident precision {accuracy.confident_precision:.1%}; "
        f"first disagreements: {accuracy.disagreements[:3]}"
    )
    # **The assertion that was missing, and the only one that could have caught it.** A
    # grader that cannot parse a language's symbols does not fail: it silently narrows the
    # population and reports a flattering number over whatever survived. The exclusion rate
    # is the one place that shows. Python has had this guard since it was written; Rust did
    # not, and ran at 73.7% for a commit. Per-language, because 30.5% is a real property of
    # `scip-python` and anything near it is not one of `rust-analyzer`.
    assert accuracy.excluded_share <= 0.05, (
        f"{accuracy.excluded_share:.1%} of Rust call sites excluded as untrustworthy; "
        "suspect `symbol_tail` before believing the oracle disagrees this often"
    )


# ---- and for Java, where the index is a build ----------------------------------------------

JAVA_CORPUS = CORPUS.parent / "java-spring-petclinic"

#: `coursier install --contrib scip-java` writes to `~/Library/Application Support/Coursier/
#: bin` (or `~/.local/share/coursier/bin`), which **is not on PATH** unless the user puts it
#: there. `shutil.which` was the whole check and reported the tool missing on a machine that
#: had just installed it, so the environment override is not a convenience here.
SCIP_JAVA = shutil.which("scip-java") or os.environ.get("OXN_SCIP_JAVA")

requires_scip_java = pytest.mark.skipif(
    not (SCIP_JAVA and shutil.which("mvn")),
    reason="scip-java and Maven are both required; scip-java runs the project's build",
)


@requires_scip_java
@pytest.mark.skipif(not JAVA_CORPUS.exists(), reason="corpora not fetched")
@pytest.mark.slow
def test_java_l2_and_l0_l1_accuracy(tmp_path) -> None:
    """The fourth language, and the one whose *cost* is the finding rather than its accuracy.

    On `java-spring-petclinic`: L2 coverage **99.6% of declarations and 100% of call sites**
    -- the highest of the four -- and L0/L1 scores **100% precision when certain** at 87.9%
    confident recall, 90.4% overall at 100% recall, with **zero** untrustworthy exclusions.

    **Read that 100% as "small", not as "solved".** It is 285 confident answers over 324
    graded call sites; Python, Go and Rust are graded on 1,453, 1,578 and 6,642. petclinic is
    50 files and 4,214 lines of Java, and a corpus that size cannot separate a resolver that
    is right from one that has not met a hard case yet. Every collision it does contain is
    handled the way the design says: `findAll` is declared in several test classes, L1 sees
    more than one candidate, and it answers with `1/n` rather than certainty -- which is why
    all 31 wrong answers are among the 39 uncertain ones. That is the Go fix from `d74cbd9`
    working, on a language it was not written for.

    **`scip-java` took 361 s on 4,214 lines**, against P5's "<60 s on a 100k-LOC repo": six
    times the budget on a repository 24 times smaller. It is not a slow indexer -- it *runs
    Maven*, so that is dependency resolution and compilation, and it would cost about the
    same for a tenth of the code. Java L2 is a batch operation. ADR-0002's fourth amendment
    carries the consequences.

    **This calls `scip-java` directly, deliberately.** petclinic declares two builds, so
    `scip-java` demands `--build-tool` and OXN passes neither (see
    `test_scip_registry.py::test_java_does_not_guess_a_build_tool`). Going through
    `run_indexer` would mean teaching OXN to guess a build system to make a test pass. This
    is the path a real user has here: `oxn index --index-file`, over an index built by hand.
    """
    import subprocess

    from oxn.graph.indexer import Indexer
    from oxn.resolve.measure import measure_corpus
    from oxn.scip.ingest import ingest_index

    corpus = JAVA_CORPUS.resolve()
    index = tmp_path / "petclinic.scip"
    built = subprocess.run(
        [SCIP_JAVA, "index", "--build-tool=maven", "--output", str(index)],
        cwd=str(corpus),
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    # Not `check=True`: this runs a six-minute Maven build, and a `CalledProcessError`
    # carrying none of Maven's output is the most expensive way to learn a dependency
    # failed to resolve.
    assert built.returncode == 0, built.stderr[-800:]

    with Indexer(root=corpus, cache_path=tmp_path / "graph.db") as indexer:
        report = ingest_index(indexer, index)
    assert report.definition_coverage >= 0.90, f"{report.definition_coverage:.1%}"
    assert report.call_coverage >= 0.85, f"{report.call_coverage:.1%}"

    accuracy = measure_corpus(corpus, index, language="java")
    # Deliberately an order of magnitude below the other corpora's floors, because the corpus
    # is: the assertion is that petclinic still parses and grades, not that 324 sites settle
    # anything about Java.
    assert accuracy.graded_call_sites > 250, "corpus too small to mean anything"
    assert accuracy.confident_precision >= 0.95, (
        f"confident precision {accuracy.confident_precision:.1%}; "
        f"first disagreements: {accuracy.disagreements[:3]}"
    )
    # The guard Rust did not have. Zero here, and a Java symbol is the simplest of the four
    # -- `pkg/Class#method().` -- so anything above the floor means the parser broke.
    assert accuracy.excluded_share <= 0.05, (
        f"{accuracy.excluded_share:.1%} of Java call sites excluded as untrustworthy; "
        "suspect `symbol_tail` before believing the oracle disagrees this often"
    )


# ---- and TypeScript, the row that found the `local` defect ---------------------------------

TS_CORPUS = CORPUS.parent / "typescript-nest"

requires_scip_typescript = pytest.mark.skipif(
    not shutil.which("scip-typescript"),
    reason="scip-typescript not installed; npm install -g @sourcegraph/scip-typescript",
)


@requires_scip_typescript
@pytest.mark.skipif(not TS_CORPUS.exists(), reason="corpora not fetched")
@pytest.mark.slow
def test_typescript_l2_and_l0_l1_accuracy(tmp_path) -> None:
    """The fifth language, and the corpus whose exclusion count was hiding a real defect.

    On `typescript-nest`: L2 coverage **99.9% of declarations and 83.6% of call sites**,
    index built in 3 s. L0/L1 scores **100% precision when certain** at 56.7% confident
    recall, 69.4% overall at 100% recall, over 2,470 graded sites -- 190 of them gradeable
    only once a callable bound to a name stopped being anonymous to the SCIP join, and 108
    more once a nameless `local N` symbol stopped being asked to agree about a name.

    **That is Python's number, on a corpus larger than Python's.** The second high row, and
    the one that makes "only Python is high" no longer the shape of the table:
    Python and TypeScript sit near 100%, Go and Rust near 90%. This test does not claim to
    know why, and deliberately: the last causal story told from these corpora was an artifact
    of a grader bug, and two rows on each side is a pattern, not a cause.

    **The 83.6% call coverage misses P5's >=85% criterion**, on the only corpus that does,
    and the 16.4% is two causes -- counted, because this docstring first asserted it was all
    the indexer's and that was wrong. **10.5% have no SCIP occurrence** at the callee, which
    is `scip-typescript`'s coverage; **5.9% have one and no enclosing entity**, because a
    module-level call (`bootstrap()` in a `main.ts`) has no caller to hang an edge on. The
    second is the criterion's denominator disagreeing with the join by design, and ADR-0002
    carries the split for every language that has one.

    **This row cost 529 fabricated L2 edges to publish, which is what it was worth.** The
    first measurement excluded 441 sites (16.9%); printing them rather than attributing them
    found that every one was a document-scoped `local N` symbol keyed globally
    (`tests/test_scip_local_symbols.py`). Exclusions are 1.08% after the fix -- the 441 were
    a double error that cancelled.

    The rest were read, not assumed: `typeLiteral268:commitOffsets` methods on anonymous type
    literals and one `<get>id` accessor -- decoration rather than disagreement. The same-file
    `local` functions listed there as a third category are graded now; `is_document_local`
    records what excluding them cost on a JavaScript corpus.
    """
    from oxn.graph.indexer import Indexer
    from oxn.resolve.measure import measure_corpus
    from oxn.scip.ingest import ingest_index
    from oxn.scip.runner import run_indexer

    corpus = TS_CORPUS.resolve()
    index = run_indexer("typescript", corpus, tmp_path / "nest.scip", timeout=600)

    with Indexer(root=corpus, cache_path=tmp_path / "graph.db") as indexer:
        report = ingest_index(indexer, index)
        # The corpus assertion for `test_scip_local_symbols.py`: nest is the repository that
        # has 628 documents defining `local 0`, so it is the one that proves the fix on real
        # output rather than on two hand-made documents.
        fabricated = indexer.store._conn.execute(  # noqa: SLF001 - a defect needs the rows
            "SELECT COUNT(*) FROM edges e JOIN symbols s ON s.symbol = e.dst_ref"
            " WHERE e.dst_ref LIKE 'local %' AND e.dst_id IS NOT NULL"
            " AND s.file_path != e.file_path"
        ).fetchone()[0]
    assert fabricated == 0, f"{fabricated} L2 edges resolved a local symbol into another file"
    assert report.definition_coverage >= 0.90, f"{report.definition_coverage:.1%}"
    assert report.call_coverage >= 0.80, f"{report.call_coverage:.1%} (below P5's 85%)"

    accuracy = measure_corpus(corpus, index, language="typescript")
    assert accuracy.graded_call_sites > 1000, "corpus too small to mean anything"
    assert accuracy.confident_precision >= 0.95, (
        f"confident precision {accuracy.confident_precision:.1%}; "
        f"first disagreements: {accuracy.disagreements[:3]}"
    )
    assert accuracy.excluded_share <= 0.05, (
        f"{accuracy.excluded_share:.1%} of TypeScript call sites excluded; this number was "
        "16.9% and the cause was a defect in OXN, not in the oracle -- read the sites"
    )


# ---- javascript ---------------------------------------------------------------------------

JS_CORPUS = CORPUS.parent / "javascript-eslint"


@requires_scip_typescript
@pytest.mark.skipif(not JS_CORPUS.exists(), reason="corpora not fetched")
@pytest.mark.slow
def test_javascript_l2_and_l0_l1_accuracy(tmp_path) -> None:
    """The sixth language, and the one whose oracle was mostly being thrown away.

    On `javascript-eslint` (1,451 JavaScript files; the 36 TypeScript ones are graded under
    TypeScript's row, not this one): L2 coverage **89.0% of declarations and 69.3% of call
    sites** over the whole corpus, from a 52 MB index built in 20 s. L0/L1 scores **99.8%
    precision when certain** at 65.3% confident recall, 74.5% overall at 100% recall, over
    **13,221 graded call sites** -- the largest graded population of the six.

    **It was 8,185 sites at 38.18% excluded until this run.** 5,036 of the 5,055 exclusions
    were `local N` symbols, which carry no descriptor and so could never satisfy a rule that
    the oracle's name match the source's. `is_document_local` records the fix and the
    measurement that it does not cost the position join anything.

    **69.3% call coverage misses P5's >=85% by more than TypeScript's 83.6% did**, and the
    30.7% is the same two causes, counted rather than attributed over the 43,777 call sites
    in JavaScript files: **15.5% have no SCIP occurrence** at the callee, and **14.7% have
    one and no enclosing entity** (69.7% joined, on that subset). The second is
    much larger here than in TypeScript's 5.9%, and it is what a JavaScript repository looks
    like -- `Makefile.js`, config files and scripts call at module level, where there is no
    caller to hang an edge on. That is the criterion's denominator disagreeing with the join
    by design, not an indexer gap; it is reported as a miss anyway, because dividing a
    criterion by a friendlier denominator after the fact is how a number stops meaning
    anything.

    The precision figures are the lowest of the six, and are not explained here for the same
    reason TypeScript's high ones are not: one corpus is not a cause.
    """
    from oxn.graph.indexer import Indexer
    from oxn.resolve.measure import measure_corpus
    from oxn.scip.ingest import ingest_index
    from oxn.scip.runner import run_indexer

    corpus = JS_CORPUS.resolve()
    index = run_indexer("javascript", corpus, tmp_path / "eslint.scip", timeout=1200)

    with Indexer(root=corpus, cache_path=tmp_path / "graph.db") as indexer:
        report = ingest_index(indexer, index)
    assert report.definition_coverage >= 0.80, f"{report.definition_coverage:.1%}"
    assert report.call_coverage >= 0.60, f"{report.call_coverage:.1%} (below P5's 85%)"

    accuracy = measure_corpus(corpus, index, language="javascript")
    assert accuracy.graded_call_sites > 10000, "corpus too small to mean anything"
    assert accuracy.confident_precision >= 0.95, (
        f"confident precision {accuracy.confident_precision:.1%}; "
        f"first disagreements: {accuracy.disagreements[:3]}"
    )
    assert accuracy.excluded_share <= 0.05, (
        f"{accuracy.excluded_share:.1%} of JavaScript call sites excluded; this was 38.18% "
        "and every one of them was a nameless `local N`, not an oracle disagreement"
    )
