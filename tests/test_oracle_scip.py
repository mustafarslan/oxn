"""SCIP: a real index from the real indexer, and L0/L1 graded against it.

These are the numbers ADR-0002's gating policy rests on -- only a *certain* answer may
block a ceiling -- so they are re-measured rather than quoted."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

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

    Re-measured 2026-09-11 by `scripts/measure_resolution.py`: index in 5 s, **99.0% of
    declarations** matched a symbol and **97.2% of call sites** (4,073 of 4,189). Of the rest,
    116 (2.8%) had no SCIP occurrence at the callee and **0** had one without an enclosing
    entity. Thresholds sit below the observed values because CI runners and indexer versions
    vary.
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

    Re-measured 2026-09-11: **100.0% precision at 65.5% recall when L1 is *certain***, 70.3% at
    100% recall when it also guesses, over 1,453 graded sites. The precision figure was
    published as 99.8% and had improved without anything failing, which is the direction this
    staleness usually runs and no less stale for it. That split is the gating policy in ADR-0002
    -- only certain answers may block.

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

    Re-measured 2026-09-11 with `scip-go` 0.2.7: **100% of declarations** matched a symbol and
    **87.8% of call sites** (5,758 of 6,558). Of the rest, 800 (12.2%) had no SCIP occurrence
    at the callee and **0** had one without an enclosing entity. The published figure was
    88.3%; it was measured with whatever `scip-go` was installed then, and this run installed
    `@latest`, so the half-point is not attributable to OXN either way.

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

    Go on go-kit, re-measured 2026-09-11: **99.4% precision when L1 is certain** at 56.7%
    confident recall, 69.7% overall at 100% recall, over 1,794 graded sites. Nothing is
    excluded -- scip-go does not contradict its own source anywhere, where scip-python does at
    30.5% of sites.

    **This row was 99.7% / 64.1% / 71.4% and drifted seven points of confident recall with
    nothing failing**, because the tests assert floors and the figures live in prose. Two
    things changed underneath it and neither is separable from here: the receiver fallback was
    removed from `scip.join` on 2026-09-10, and `scip-go` was reinstalled at `@latest` (0.2.7)
    on 2026-09-11, which moves the ground truth itself. `scripts/measure_resolution.py` exists
    so the next re-measurement is a command rather than a reconstruction.

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

    On ripgrep, re-measured 2026-09-11: L2 coverage 99.2% of declarations and **99.0% of call
    sites** (13,367 of 13,505), index built in 40 s against the 60 s budget. Of the rest, 138
    (1.0%) had no SCIP occurrence and **0** had one without an enclosing entity. L0/L1 scores
    **99.1% precision when certain** at 42.1% confident recall; 52.8% overall at 100% recall,
    over 6,665 graded call sites.

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
    index = run_indexer("rust", corpus, tmp_path / "ripgrep.scip")

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
    from oxn.scip.runner import INDEXERS

    corpus = JAVA_CORPUS.resolve()
    index = tmp_path / "petclinic.scip"
    built = subprocess.run(
        [SCIP_JAVA, "index", "--build-tool=maven", "--output", str(index)],
        cwd=str(corpus),
        capture_output=True,
        text=True,
        # Matches `INDEXERS['java'].timeout`; this path calls `scip-java` directly because
        # petclinic declares two builds, but it is paying for the same Maven run.
        timeout=INDEXERS["java"].timeout,
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


def _source_coverage(corpus: Path, index: Path) -> float:
    """Call coverage over the files that are not tests.

    **The population P5's criterion was stated over.** A coverage ratio describes whichever
    files the index happened to contain, so widening the index moves it without anything
    about resolution having changed -- and a guard that cannot tell those apart stops being a
    guard the first time someone indexes more. On `typescript-nest` the split is exact:
    `.spec.ts` is the suffix its `tsconfig.spec.json` selects on.
    """
    from oxn.graph.builder import build_file
    from oxn.languages import get_parser
    from oxn.profiles import profile_for_path
    from oxn.scip.index import load_index
    from oxn.scip.join import join_document

    sites = joined = 0
    for document in load_index(index).documents:
        if document.relative_path.endswith(".spec.ts"):
            continue
        source_path = corpus / document.relative_path
        profile = profile_for_path(document.relative_path)
        if profile is None or not source_path.exists():
            continue
        source = source_path.read_bytes()
        tree = get_parser(profile.name).parse(source)
        if tree.root_node.has_error:
            continue
        parsed = build_file(document.relative_path, source, profile, tree.root_node)
        result = join_document(list(parsed.entities), profile, tree.root_node, document)
        sites += result.call_sites
        joined += result.joined_call_sites
    return joined / sites if sites else 0.0


@requires_scip_typescript
@pytest.mark.skipif(not TS_CORPUS.exists(), reason="corpora not fetched")
@pytest.mark.slow
def test_typescript_l2_and_l0_l1_accuracy(tmp_path) -> None:
    """The fifth language, and the corpus whose exclusion count was hiding a real defect.

    On `typescript-nest`: L2 coverage **98.6% of declarations and 33.0% of call sites**,
    index built in 4 s. L0/L1 scores **99.7% precision when certain** at 58.0% confident
    recall, 69.8% overall at 100% recall, over 6,240 graded sites.

    **Those are the numbers since the index began covering the tests, on 2026-09-17, and the
    coverage figure got worse for a good reason.** `scip-typescript` indexes the `tsconfig.json`
    it is handed; nest declares its tests in a sibling `tsconfig.spec.json` that nothing asked
    it to read, so 424 `*.spec.ts` files had no L2 symbols at all and the measured population
    was 1,020 files of 1,913. `Indexer.projects` now passes every root `tsconfig*.json`: 1,444
    files, 40,359 call sites, **91.0% of the tree** against 18.5%. Joined call sites more than
    doubled, 6,233 -> 13,337, and the graded population went 2,470 -> 6,240.

    **The ratio fell because the population changed, not the resolver.** Split on the same
    index: non-test files join at **76.2%**, the identical figure to before, and the 424 test
    files at **22.1%** over 32,175 sites. Roughly half their call heads are `expect` (6,083),
    `it` (3,894), `describe` (2,164) and vitest matchers -- framework code outside this tree,
    which nothing inside it can be joined to. That is why the assertions below are three: the
    original 70% guard over non-test files, where P5's criterion was stated; a floor on the
    absolute joined count, which is what a reverted fix trips and no ratio would; and an honest
    project-wide floor.

    **Precision fell 100% -> 99.7% and that is a finding, not noise.** Eleven of 3,622 confident
    answers are wrong, every one a generic method name -- `send`, `get`, `create`, `update`,
    `remove`, `findOne`, `findAll` -- inside `integration/`, where a dozen independent sample
    applications each define their own `ConfigService.get` and `AppController.send`.
    Unique-name resolution is only as unique as the file set it runs over, and widening the set
    is what made that visible. Not fixed here.

    **Confident recall was 56.7% until 2026-09-14, and two changes moved it, both aimed at
    ECMAScript's idioms.** Following a barrel is worth eight: nest has 96 files that are nothing
    but `export * from "./x"` and 451 such edges nested two deep, and a lookup that stopped at
    the file an import named found nothing in them. Resolving a *class*-qualified call against
    the class rather than the file's top-level declarations is worth nineteen more -- see
    `symbols._member_of`. Confident answers 1,402 -> 1,429, every one of the twenty-seven
    correct, precision unmoved at 100%. Python, Go, Rust and Java do not move at all, which is
    what changes aimed at one language's idioms should look like.

    **That is Python's number, on a corpus larger than Python's.** The second high row, and
    the one that makes "only Python is high" no longer the shape of the table:
    Python and TypeScript sit near 100%, Go and Rust near 90%. This test does not claim to
    know why, and deliberately: the last causal story told from these corpora was an artifact
    of a grader bug, and two rows on each side is a pattern, not a cause.

    **The call coverage misses P5's >=85% criterion, and the cause is one thing.**
    Recounted 2026-09-11 by `IngestReport`, which reports the split now rather than leaving it
    to a script: of 8,184 call sites then indexed, 6,233 joined and **1,951 (23.8%) had no SCIP
    occurrence at the callee**. Over the wider 2026-09-17 population it is 40,359 sites, 13,337
    joined and **27,022 (67.0%) with no occurrence**. Sites with an occurrence and no enclosing
    entity: **0**, before and after.

    That last number is the correction. This docstring said 10.5% and 5.9%, and the second
    figure -- "a module-level call has no caller to hang an edge on" -- stopped being true when
    `join._map_calls` began attributing such a call to the file's own entity. The explanation
    outlived the defect it explained, in both this row and JavaScript's, and got repeated into
    the ROADMAP. The gap is `scip-typescript`'s occurrence coverage and nothing else.

    **And 76.2% is of the files the indexer covered, which here is 1,020 of 1,913.** Ingest
    walks the *index's* documents, so a file with no document contributes no call sites --
    neither joined nor missed. nest's uncovered 893 hold **36,364 further call sites**, so the
    figure describes 8,184 of 44,548, or 18% of the tree; joined against everything OXN parses
    it is 14.0%. The measurement is right -- you cannot join what was not indexed -- and the
    denominator was invisible, which is a different fault and the one worth fixing.
    `IngestReport.unindexed_files` reports it now.

    The 893 are not arbitrary: **every one of the 277 under `packages/` is a `*.spec.ts`**,
    because `scip-typescript` indexes the source `tsconfig` and not the test one. The rest are
    448 `sample/` applications with their own configs, 147 `integration/` files and 21 of
    tooling. So this is the shape of a TypeScript monorepo rather than a defect in either tool
    -- and a criterion stated as "call sites resolved" still has to say which call sites.

    **This row cost 529 fabricated L2 edges to publish, which is what it was worth.** The
    first measurement excluded 441 sites (16.9%); printing them rather than attributing them
    found that every one was a document-scoped `local N` symbol keyed globally
    (`tests/test_scip_local_symbols.py`). Exclusions are 1.08% after the fix -- the 441 were
    a double error that cancelled.

    The rest were read, not assumed: `typeLiteral268:commitOffsets` methods on anonymous type
    literals and one `<get>id` accessor -- decoration rather than disagreement. The same-file
    `local` functions listed there as a third category are graded now; `is_document_local`
    records what excluding them cost on a JavaScript corpus.

    **The call-coverage figure fell on 2026-09-10, and the drop is a correction rather than a
    regression.** `_occurrence_at` used to fall back to the callee's *start* position when the
    last name component had no occurrence -- and for a qualified callee the start belongs to
    the receiver, so the join hit `sourceCode` rather than `getTokenAfter`, `espree` rather
    than `tokenize`, `Object` rather than `assign`. Each of those counted as a joined call
    site. Measured across four corpora, the fallback contributed **18,607 call edges of which
    64 resolved to anything -- 0.34%**:

    | corpus | coverage with | without | edges added | resolved |
    |---|---|---|---|---|
    | python-httpx | 99.7% | 97.2% | 103 | 0 |
    | oxn (self) | 98.8% | 89.4% | 826 | 7 |
    | typescript-nest | 89.5% | 76.2% | 1,091 | 9 |
    | javascript-eslint | 84.6% | 47.0% | 16,587 | 48 |

    So the criterion had been reading "did an occurrence exist anywhere inside the callee
    expression", which is not the question. JavaScript falls furthest because it is the
    corpus with the least type information for an indexer to place a property name with, and
    that is the honest shape of the gap rather than a number to divide differently.
    """
    from oxn.graph.indexer import Indexer
    from oxn.resolve.measure import measure_corpus
    from oxn.scip.ingest import ingest_index
    from oxn.scip.runner import run_indexer

    corpus = TS_CORPUS.resolve()
    index = run_indexer("typescript", corpus, tmp_path / "nest.scip")

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

    # **Three assertions, because one ratio cannot separate a worse join from a wider net.**
    # Indexing `tsconfig.spec.json` alongside `tsconfig.json` brought 424 test files in, and
    # they carry 32,175 call sites that join at 22.1% -- so the project-wide figure fell from
    # 76.2% to 33.0% while the resolver did not change at all.
    assert _source_coverage(corpus, index) >= 0.70, (
        "L2 coverage of non-test files regressed; this is the population P5's criterion was "
        "stated over and it is unaffected by which tsconfigs are indexed"
    )
    # The absolute count is what notices the fix being lost: drop the `tsconfig*.json`
    # discovery in `scip.runner` and this falls back to 6,233, which no ratio would flag.
    assert report.joined_call_sites >= 13_000, f"{report.joined_call_sites:,} joined call sites"
    assert report.call_coverage >= 0.30, (
        f"{report.call_coverage:.1%} project-wide. Test files join at 22.1% because roughly "
        "half their call heads are `expect`, `it`, `describe` and vitest matchers -- framework "
        "code that is not in this tree and so cannot be joined to anything in it."
    )

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
    TypeScript's row, not this one): L2 coverage **87.6% of declarations and 52.1% of call
    sites** over the whole corpus, from a 54 MB index built in 22 s. L0/L1 scores **99.8%
    precision when certain** at 65.4% confident recall, 74.5% overall at 100% recall, over
    **13,289 graded call sites** -- the largest graded population of the six.

    **Every JavaScript figure published before 2026-09-17 was measured against a config OXN
    wrote itself.** `--infer-tsconfig` writes a `tsconfig.json` where it finds none, and
    eslint's was one line -- `{"allowJs": true}` -- sitting untracked in the corpus, dated to
    the day this entry was fixed. The repository's own `tsconfig.base.json` sets `checkJs`,
    `strict` and NodeNext resolution, and reading it instead moves call coverage **47.0% ->
    52.1%**: joined sites 20,754 -> 23,009, resolved CALLS edges 13,317 -> 13,558, and every
    directory improves -- `lib/` most, **51.9% -> 59.9%**. `Indexer.projects` passes the
    project's root configs, and `Indexer.leaves_behind` deletes the inferred stub again,
    because a tool that measures a repository may not modify it.

    **Two costs, both stated rather than smoothed.** Declaration coverage falls 89.0% ->
    87.6%, 67 fewer joined definitions; `types: ["node"]` cannot resolve without an installed
    `node_modules`, so some of the base config's intent is unavailable here and the gain is
    largely `checkJs` alone. REFERENCES edges fall 7,339 -> 5,500, which sounds worse than it
    is: the dead-code surface reads those for reachability and reports **0 candidates on
    eslint either way**. The two `tsconfig.types*.json` configs contribute nothing at all; the
    whole gain is `tsconfig.base.json`.

    **`tests/` is 71% of this corpus's call sites** -- 31,551 of 44,141, joining at 48.7% --
    so the project-wide figure is mostly a statement about mocha and assert. Unlike nest,
    though, that is not the whole story: `lib/` at 59.9% is still the lowest source-code row
    in the table, and that residue belongs to the indexer rather than to the configuration.

    **It was 8,185 sites at 38.18% excluded until this run.** 5,036 of the 5,055 exclusions
    were `local N` symbols, which carry no descriptor and so could never satisfy a rule that
    the oracle's name match the source's. `is_document_local` records the fix and the
    measurement that it does not cost the position join anything.

    **47.0% call coverage is the largest miss of the six**, by a distance, and it is one
    cause rather than two. Recounted 2026-09-11 from `IngestReport`: of 44,141 call sites,
    20,754 joined and **23,387 (53.0%) had no SCIP occurrence at the callee**. Sites with an
    occurrence and no enclosing entity: **0**.

    This row previously read 15.5% and 14.7%, and attributed the larger half to module-level
    calls in `Makefile.js`, config files and scripts -- "the criterion's denominator
    disagreeing with the join by design, not an indexer gap". That is now exactly backwards.
    Such a call is attributed to the file's own entity since `join._map_calls` gained that
    fallback, so the denominator agrees; what remains is the indexer having no occurrence to
    join to, which *is* an indexer gap, and the honest reading is that JavaScript is the corpus
    with the least type information for one to place a property name with.

    The split is reported by the tool now (`calls_without_occurrence`, `calls_without_caller`)
    rather than recomputed by a script and pasted here, which is what let the old explanation
    outlive the defect it explained.

    **JavaScript's denominator, unlike TypeScript's, is nearly the whole tree**: 1,463 of 1,470
    documents matched and only 18 project files are unindexed, holding 126 call sites between
    them. So 52.1% really is 52% of eslint, and the contrast with nest -- where the same figure
    covered 18% of the tree until its tests were indexed -- is why `IngestReport.unindexed_files`
    exists.

    The precision figures are the lowest of the six, and are not explained here for the same
    reason TypeScript's high ones are not: one corpus is not a cause.

    **The call-coverage figure fell on 2026-09-10, and the drop is a correction rather than a
    regression.** `_occurrence_at` used to fall back to the callee's *start* position when the
    last name component had no occurrence -- and for a qualified callee the start belongs to
    the receiver, so the join hit `sourceCode` rather than `getTokenAfter`, `espree` rather
    than `tokenize`, `Object` rather than `assign`. Each of those counted as a joined call
    site. Measured across four corpora, the fallback contributed **18,607 call edges of which
    64 resolved to anything -- 0.34%**:

    | corpus | coverage with | without | edges added | resolved |
    |---|---|---|---|---|
    | python-httpx | 99.7% | 97.2% | 103 | 0 |
    | oxn (self) | 98.8% | 89.4% | 826 | 7 |
    | typescript-nest | 89.5% | 76.2% | 1,091 | 9 |
    | javascript-eslint | 84.6% | 47.0% | 16,587 | 48 |

    So the criterion had been reading "did an occurrence exist anywhere inside the callee
    expression", which is not the question. JavaScript falls furthest because it is the
    corpus with the least type information for an indexer to place a property name with, and
    that is the honest shape of the gap rather than a number to divide differently.
    """
    from oxn.graph.indexer import Indexer
    from oxn.resolve.measure import measure_corpus
    from oxn.scip.ingest import ingest_index
    from oxn.scip.runner import run_indexer

    corpus = JS_CORPUS.resolve()
    index = run_indexer("javascript", corpus, tmp_path / "eslint.scip")

    with Indexer(root=corpus, cache_path=tmp_path / "graph.db") as indexer:
        report = ingest_index(indexer, index)
    assert report.definition_coverage >= 0.80, f"{report.definition_coverage:.1%}"
    assert report.call_coverage >= 0.42, (
        f"{report.call_coverage:.1%}, still short of P5's 85%. `tests/` is 71% of this "
        "corpus's call sites and joins at 48.7%, so the project-wide figure is largely a "
        "statement about mocha and assert -- but `lib/` at 59.9% is the lowest source-code "
        "row in the table, and that residue is the indexer's."
    )
    # The absolute count, because a ratio cannot tell a reverted config from a worse join:
    # drop `projects` from the JavaScript entry and this falls back to 20,754.
    assert report.joined_call_sites >= 22_500, (
        f"{report.joined_call_sites:,} joined call sites; eslint's own `tsconfig.base.json` "
        "is what takes this past 22,500, and an inferred stub is what it falls back to"
    )

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
