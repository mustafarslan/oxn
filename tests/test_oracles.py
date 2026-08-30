"""Differential tests against the free tools OXN deliberately does not depend on.

This lane is what makes "OXN computes its own metrics" a *checkable* claim rather than an
assertion (ADR-0001). It is marked ``oracle`` and excluded from the default lane, because
these tools must never become runtime dependencies:

    pytest -m oracle          # needs: pip install -e ".[dev,oracle]"

Assertions apply the documented divergences in ``docs/divergences.md`` rather than
expecting raw equality -- the oracles disagree with *each other*, so a suite demanding
equality against all of them is red forever.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from oxn.languages import get_parser
from oxn.metrics import cognitive_complexity, cyclomatic_complexity, halstead
from oxn.profiles import get_profile

pytestmark = pytest.mark.oracle

radon_complexity = pytest.importorskip("radon.complexity")
radon_metrics = pytest.importorskip("radon.metrics")
complexipy = pytest.importorskip("complexipy")
lizard = pytest.importorskip("lizard")

CORPUS = Path("benchmarks/corpora/python-httpx")

#: Constructs where radon and lizard disagree with each other, so OXN cannot match both.
#: Each entry is (source, radon, lizard, oxn) and is asserted exactly -- if a tool changes
#: its behaviour, this fails and docs/divergences.md gets revisited.
DOCUMENTED_CYCLOMATIC_DIVERGENCES = [
    ("def f(a):\n    assert a\n", 2, 1, 2),
    ("def f():\n    try: pass\n    finally: pass\n", 1, 2, 1),
    ("def f(a):\n    for i in a: pass\n    else: pass\n", 3, 2, 2),
    # `case _` is the fall-through, not a branch: OXN scores this the same as the
    # `if`/`else` it is equivalent to, agreeing with radon against lizard.
    ("def f(a):\n    match a:\n        case 1: pass\n        case _: pass\n", 2, 3, 2),
]


def _oxn_cyclomatic(source: str) -> int:
    profile = get_profile("python")
    root = get_parser("python").parse(source.encode()).root_node
    return cyclomatic_complexity(profile.unwrap(root.named_children[0]), profile)


def _oxn_functions(path: Path) -> dict[str, int] | None:
    """Leaf name -> cognitive score for every function in a file."""
    profile = get_profile("python")
    root = get_parser("python").parse(path.read_bytes()).root_node
    if root.has_error:
        return None
    scores: dict[str, int] = {}

    def walk(node) -> None:
        for child in node.named_children:
            definition = profile.unwrap(child)
            if definition.type in profile.function_like or definition.type in profile.class_like:
                name = profile.entity_name(definition)
                if name and definition.type in profile.function_like:
                    scores[name] = cognitive_complexity(
                        definition, profile, function_name=name
                    ).score
                body = definition.child_by_field_name(profile.body_field)
                if body is not None:
                    walk(body)
            else:
                walk(child)

    walk(root)
    return scores


@pytest.mark.parametrize(
    ("source", "expected_radon", "expected_lizard", "expected_oxn"),
    DOCUMENTED_CYCLOMATIC_DIVERGENCES,
)
def test_documented_cyclomatic_divergences_still_hold(
    source: str, expected_radon: int, expected_lizard: int, expected_oxn: int
) -> None:
    """The oracles disagree here; OXN picks a side and says which, in docs/divergences.md."""
    got_radon = sum(block.complexity for block in radon_complexity.cc_visit(source))
    got_lizard = sum(
        fn.cyclomatic_complexity
        for fn in lizard.analyze_file.analyze_source_code("t.py", source).function_list
    )
    assert got_radon == expected_radon, "radon changed behaviour; revisit docs/divergences.md"
    assert got_lizard == expected_lizard, "lizard changed behaviour; revisit docs/divergences.md"
    assert _oxn_cyclomatic(source) == expected_oxn


@pytest.mark.skipif(not CORPUS.exists(), reason="corpora not fetched")
@pytest.mark.slow
def test_cognitive_agrees_with_complexipy_on_a_real_codebase() -> None:
    """Exit criterion for the cognitive-complexity implementation: >= 95% agreement.

    Measured 99.24% on httpx (651/656 functions). The remaining five are all one cause --
    complexipy misses comprehensions in some expression positions -- and are enumerated in
    docs/divergences.md.
    """
    compared = agreed = 0
    disagreements: list[str] = []

    for path in sorted(CORPUS.rglob("*.py")):
        ours = _oxn_functions(path)
        if ours is None:
            continue
        try:
            theirs = {
                fn.name: fn.complexity for fn in complexipy.file_complexity(str(path)).functions
            }
        except Exception:  # noqa: BLE001 -- an oracle failure must not fail our suite
            continue
        for name, expected in theirs.items():
            if name not in ours:
                continue
            compared += 1
            if ours[name] == expected:
                agreed += 1
            else:
                disagreements.append(f"{path.name}::{name} oxn={ours[name]} complexipy={expected}")

    assert compared > 300, f"only {compared} functions compared; corpus too small to be meaningful"
    ratio = agreed / compared
    assert ratio >= 0.95, f"agreement {ratio:.2%} ({agreed}/{compared}); first: {disagreements[:5]}"


@pytest.mark.skipif(not CORPUS.exists(), reason="corpora not fetched")
@pytest.mark.slow
def test_halstead_volume_tracks_radon() -> None:
    """Rank correlation only -- radon classifies a strictly narrower operator set.

    On ``def f(a, b): return a + b * 2`` radon sees two operators, OXN sees six. Equality is
    meaningless here; that the two move together is still worth knowing. See
    docs/divergences.md.
    """
    profile = get_profile("python")
    parser = get_parser("python")
    ours: list[float] = []
    theirs: list[float] = []

    for path in sorted((CORPUS / "httpx").rglob("*.py")):
        source = path.read_bytes()
        root = parser.parse(source).root_node
        if root.has_error:
            continue
        try:
            reference = radon_metrics.h_visit(source.decode())
        except Exception:  # noqa: BLE001
            continue
        ours.append(halstead(root, profile).volume)
        theirs.append(reference.total.volume)

    assert len(ours) > 10
    assert _spearman(ours, theirs) >= 0.75


def _spearman(first: list[float], second: list[float]) -> float:
    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        for position, index in enumerate(order):
            out[index] = position + 1
        return out

    a, b = ranks(first), ranks(second)
    n = len(a)
    mean_a, mean_b = sum(a) / n, sum(b) / n
    covariance = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=True))
    spread_a = sum((x - mean_a) ** 2 for x in a) ** 0.5
    spread_b = sum((y - mean_b) ** 2 for y in b) ** 0.5
    return covariance / (spread_a * spread_b)


# ---- JavaScript: eslint-plugin-sonarjs ------------------------------------------------

SONARJS_DIR = os.environ.get("OXN_SONARJS_DIR")
RUNNER = Path(__file__).parent / "oracles" / "sonarjs_runner.mjs"
ESLINT_CONFIG = Path(__file__).parent / "oracles" / "eslint.config.mjs"

requires_sonarjs = pytest.mark.skipif(
    not SONARJS_DIR or not (Path(SONARJS_DIR) / "node_modules").exists(),
    reason="set OXN_SONARJS_DIR to a directory with eslint + eslint-plugin-sonarjs installed",
)

#: Cases where OXN follows the published specification and sonarjs 4.2.0 does not.
#: Each is adjudicated in docs/divergences.md; the third is agreement, kept as a guard.
SONARJS_DIVERGENCES = {
    # The white paper prints this exact expression as 4: +1 for the `if`, +1 per operator run.
    "function f(a,b,c,d,e,g){ if(a&&b&&c||d||e&&g){} }": (4, 3),
    # Appendix B lists "each method in a recursion cycle"; sonarjs does not implement it.
    "function f(a){ return f(a-1); }": (1, None),
}


@functools.lru_cache(maxsize=1)
def _staged_runner() -> tuple[Path, Path]:
    """Copy the runner and its config beside ``node_modules``.

    ESLint resolves ``eslint-plugin-sonarjs`` relative to the config file, so both must sit
    in the directory where the packages were installed.
    """
    target = Path(SONARJS_DIR or ".")
    runner = target / RUNNER.name
    config = target / ESLINT_CONFIG.name
    shutil.copyfile(RUNNER, runner)
    shutil.copyfile(ESLINT_CONFIG, config)
    return runner, config


def _sonarjs_scores(source: str) -> list[int]:
    runner, config = _staged_runner()
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, dir=SONARJS_DIR) as handle:
        handle.write(source)
        path = handle.name
    try:
        result = subprocess.run(
            ["node", str(runner), path, str(config)],
            capture_output=True,
            text=True,
            cwd=SONARJS_DIR,
            check=True,
        )
        return sorted(item["score"] for item in json.loads(result.stdout.strip() or "[]"))
    finally:
        os.unlink(path)


def _oxn_js_scores(source: str) -> list[int]:
    profile = get_profile("javascript")
    root = get_parser("javascript").parse(source.encode()).root_node
    scores: list[int] = []

    def walk(node) -> None:
        for child in node.named_children:
            definition = profile.unwrap(child)
            if definition.type in profile.function_like:
                scores.append(
                    cognitive_complexity(
                        definition, profile, function_name=profile.entity_name(definition)
                    ).score
                )
            walk(child)

    walk(root)
    return sorted(score for score in scores if score > 0)


@requires_sonarjs
@pytest.mark.parametrize(
    "source",
    [
        "function f(a){ if(a){} }",
        "function f(a){ if(a){} else {} }",
        "function f(a){ if(a){} else if(a){} else {} }",
        "function f(a){ if(a){} else if(a){ if(a){} } }",
        "function f(a){ if(a){} else if(a){} else if(a){ for(const x of a){ if(x){} } } }",
        "function f(a,b){ if(a>0){ for(let i=0;i<a;i++){ if(b>i){ g(); } } } }",
        "function f(a){ switch(a){ case 1: break; case 2: break; default: break; } }",
        "function f(){ try{} catch(e){} finally{} }",
        "function f(a){ try{ if(a){} } catch(e){ if(a){} } }",
        "function f(a,b,c){ if(a && !(b && c)){} }",
        "function f(a){ if(a > 0){} }",
        "function f(a){ return a ? 1 : 2; }",
        "function f(a){ while(a){} do {} while(a); }",
        "function f(a){ outer: for(const x of a){ for(const y of a){ break outer; } } }",
        "function f(a){ if(a){ if(a){ if(a){ if(a){} } } } }",
    ],
)
def test_javascript_matches_sonarjs(source: str) -> None:
    assert _oxn_js_scores(source) == _sonarjs_scores(source)


@requires_sonarjs
@pytest.mark.parametrize("source", sorted(SONARJS_DIVERGENCES))
def test_documented_sonarjs_divergences_still_hold(source: str) -> None:
    """OXN follows the specification here and sonarjs does not. See docs/divergences.md."""
    expected_oxn, expected_sonarjs = SONARJS_DIVERGENCES[source]
    scores = _sonarjs_scores(source)
    assert _oxn_js_scores(source) == [expected_oxn]
    if expected_sonarjs is None:
        assert scores == [], "sonarjs implemented this; revisit docs/divergences.md"
    else:
        assert scores == [expected_sonarjs], "sonarjs changed; revisit docs/divergences.md"


# ---- cyclomatic at corpus scale --------------------------------------------------------


def _count_kinds(node, profile, kinds: set[str]) -> int:
    """Occurrences of ``kinds`` inside one function, excluding nested definitions."""
    total = 0
    stack = list(node.named_children)
    while stack:
        current = profile.unwrap(stack.pop())
        if profile.is_definition(current):
            continue
        if current.type in kinds:
            total += 1
        stack.extend(current.named_children)
    return total


@pytest.mark.skipif(not CORPUS.exists(), reason="corpora not fetched")
@pytest.mark.slow
def test_every_cyclomatic_divergence_from_lizard_is_explained() -> None:
    """Raw agreement is meaningless here; explained divergence is the real criterion.

    radon and lizard contradict *each other* on constructs that appear in most real files,
    so no implementation can agree with both. What OXN must guarantee is that every
    difference reduces to a rule enumerated in docs/divergences.md:

        lizard == oxn - (asserts) + (finally clauses)

    Measured on httpx: raw agreement 56%, explained 100% of 1,134 functions. `assert` is
    simply pervasive in real Python.
    """
    profile = get_profile("python")
    parser = get_parser("python")
    compared = explained = 0
    unexplained: list[str] = []

    for path in sorted(CORPUS.rglob("*.py")):
        source = path.read_text(errors="replace")
        root = parser.parse(source.encode()).root_node
        if root.has_error:
            continue
        by_line: dict[int, int] = {}
        for function in lizard.analyze_file.analyze_source_code("t.py", source).function_list:
            by_line.setdefault(function.start_line, function.cyclomatic_complexity)

        def visit(node, by_line: dict[int, int], name: str) -> None:
            nonlocal compared, explained
            for child in node.named_children:
                definition = profile.unwrap(child)
                if definition.type in profile.function_like:
                    line = definition.start_point[0] + 1
                    if line in by_line:
                        compared += 1
                        ours = cyclomatic_complexity(definition, profile)
                        asserts = _count_kinds(definition, profile, {"assert_statement"})
                        finallys = _count_kinds(definition, profile, {"finally_clause"})
                        if ours - asserts + finallys == by_line[line]:
                            explained += 1
                        else:
                            unexplained.append(f"{name}:{line} oxn={ours} lizard={by_line[line]}")
                body = (
                    definition.child_by_field_name(profile.body_field)
                    if definition.type in (profile.function_like | profile.class_like)
                    else None
                )
                visit(body if body is not None else child, by_line, name)

        visit(root, by_line, path.name)

    assert compared > 500, f"only {compared} functions compared"
    ratio = explained / compared
    assert ratio >= 0.99, f"explained {ratio:.2%} ({explained}/{compared}); {unexplained[:5]}"


# ---- duplication: PMD-CPD --------------------------------------------------------------

PMD = Path(os.environ.get("OXN_PMD_DIR", "tools/pmd-bin")) / "bin" / "pmd"

requires_pmd = pytest.mark.skipif(
    not PMD.exists(),
    reason="PMD not installed; see scripts/check.py --oracle --install",
)


def _pmd_clone_lines(directory: Path, min_tokens: int) -> set[tuple[str, int]]:
    """(path, line) for every line PMD-CPD reports as duplicated."""
    import csv
    import io

    result = subprocess.run(
        [
            str(PMD),
            "cpd",
            "--minimum-tokens",
            str(min_tokens),
            "--language",
            "python",
            "--dir",
            str(directory),
            "--format",
            "csv",
        ],
        capture_output=True,
        text=True,
    )
    lines: set[tuple[str, int]] = set()
    for row in csv.reader(io.StringIO(result.stdout)):
        if not row or row[0] == "lines":
            continue
        span = int(row[0])
        rest = row[3:]
        for index in range(0, len(rest) - 1, 2):
            start = int(rest[index])
            path = Path(rest[index + 1]).relative_to(directory).as_posix()
            lines.update((path, start + offset) for offset in range(span))
    return lines


def _oxn_clone_lines(directory: Path, min_tokens: int) -> set[tuple[str, int]]:
    from oxn.graph.sources import iter_source_files
    from oxn.profiles import profile_for_path
    from oxn.volume.clones import file_windows, find_clones

    windows = {}
    for path in iter_source_files([directory]):
        profile = profile_for_path(str(path))
        if profile is None:
            continue
        tree = get_parser(profile.name).parse(path.read_bytes())
        if tree.root_node.has_error:
            continue
        relative = path.resolve().relative_to(directory).as_posix()
        windows[relative] = file_windows(relative, tree.root_node, profile, min_tokens=min_tokens)

    report = find_clones(windows, min_tokens=min_tokens, min_lines=4)
    return {
        (clone.path, line)
        for klass in report.clone_classes
        for clone in klass.occurrences
        for line in clone.lines
    }


@requires_pmd
@pytest.mark.skipif(not CORPUS.exists(), reason="corpora not fetched")
@pytest.mark.slow
def test_duplication_recovers_what_pmd_cpd_finds() -> None:
    """Recall against PMD-CPD, not equality.

    The two tools tokenize differently and select overlapping candidates differently, so
    exact clone-class equality is not a meaningful target. What matters for a duplication
    detector is that it does not *miss* things: measured on httpx, OXN recovers 92% of the
    lines PMD reports and flags more besides. See docs/divergences.md.
    """
    directory = (CORPUS / "httpx").resolve()
    theirs = _pmd_clone_lines(directory, 50)
    ours = _oxn_clone_lines(directory, 50)

    assert theirs, "PMD reported nothing; the oracle is misconfigured"
    recall = len(ours & theirs) / len(theirs)
    jaccard = len(ours & theirs) / len(ours | theirs)

    assert recall >= 0.85, f"recall {recall:.2%} of PMD's duplicated lines"
    assert jaccard >= 0.50, f"line-level Jaccard {jaccard:.2f}"


@requires_pmd
@pytest.mark.skipif(not CORPUS.exists(), reason="corpora not fetched")
@pytest.mark.slow
def test_duplication_agrees_with_pmd_on_which_files_are_affected() -> None:
    directory = (CORPUS / "httpx").resolve()
    theirs = {path for path, _ in _pmd_clone_lines(directory, 50)}
    ours = {path for path, _ in _oxn_clone_lines(directory, 50)}
    jaccard = len(ours & theirs) / len(ours | theirs)
    assert jaccard >= 0.60, f"file-level Jaccard {jaccard:.2f}: {sorted(theirs ^ ours)}"


# ---- import graph: grimp -----------------------------------------------------------------


def _to_module(path: str) -> str:
    """Repo-relative path -> dotted module name, the way grimp names things."""
    trimmed = path[len("src/") :] if path.startswith("src/") else path
    trimmed = trimmed[:-3] if trimmed.endswith(".py") else trimmed
    if trimmed.endswith("/__init__"):
        trimmed = trimmed[: -len("/__init__")]
    return trimmed.replace("/", ".")


def _oxn_module_edges(
    root: Path, sources: Path, package: str, *, include_type_only: bool = False
) -> set[tuple[str, str]]:
    from oxn.graph.depgraph import build_dependency_graph
    from oxn.graph.sources import iter_source_files

    files = [
        path
        for path in iter_source_files([sources])
        if "/tests/" not in str(path) and path.name != "setup.py"
    ]
    graph = build_dependency_graph(root, files, include_type_only=include_type_only)
    edges = {
        (_to_module(source), _to_module(target))
        for source, targets in graph.files.items()
        for target in targets
    }
    return {
        (source, target)
        for source, target in edges
        if source.startswith(package) and target.startswith(package)
    }


@pytest.mark.oracle
def test_every_import_divergence_from_grimp_is_explained() -> None:
    """Two mechanisms, two definitions of "depends on", and both are defensible.

    Ours reads syntax; grimp executes imports. Agreement is strong evidence, and this
    comparison has already found one real modelling bug -- `from pkg import core` reaches the
    package *and* the submodule, and treating it as one target silently lost edges.

    Raw equality is the wrong assertion, for the same reason it was wrong for cyclomatic
    complexity: the two tools answer different questions in two enumerable places.

    * **grimp counts type-only imports; OXN does not.** An import inside `if TYPE_CHECKING:`
      is erased at runtime, so it creates no coupling of the kind Martin's metrics measure --
      and OXN already excluded TypeScript's `import type`. Counting Python's equivalent was
      an inconsistency, and while it lasted it manufactured two false layer-contract
      violations in this repository.
    * **OXN records the package as well as the submodule.** `from oxn import thresholds`
      executes `oxn/__init__.py`, so the dependency on the package is real; grimp reports
      only `oxn.thresholds`.

    Every divergence must reduce to one of those two rules. A third kind is a bug.
    """
    grimp = pytest.importorskip("grimp")
    graph = grimp.build_graph("oxn", include_external_packages=False)
    theirs = {
        (module, imported)
        for module in graph.modules
        for imported in graph.find_modules_directly_imported_by(module)
    }
    assert theirs, "grimp found no modules; oxn is not importable in this environment"

    ours = _oxn_module_edges(Path.cwd(), Path("src"), "oxn")
    with_type_only = _oxn_module_edges(Path.cwd(), Path("src"), "oxn", include_type_only=True)

    # Rule 1: grimp sees it, we see it only when type-only imports are included.
    unexplained_theirs = (theirs - ours) - with_type_only
    assert not unexplained_theirs, (
        f"grimp found edges that are not type-only imports: {sorted(unexplained_theirs)}"
    )

    # Rule 2: ours names a package whose submodule edge we also record.
    unexplained_ours = {
        (source, target)
        for source, target in ours - theirs
        if not _is_package_init_edge(source, target, ours)
    }
    assert not unexplained_ours, (
        f"OXN found edges grimp did not, that are not package-init edges: "
        f"{sorted(unexplained_ours)}"
    )

    # And the rules must not be doing all the work: the bulk has to actually agree.
    agreement = len(ours & theirs) / len(theirs)
    assert agreement > 0.75, f"only {agreement:.0%} of grimp's edges are shared"


# ---- SCIP: a real index from the real indexer ---------------------------------------------

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
    from oxn.scip.runner import run_indexer

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

    output = run_indexer("python", tmp_path, tmp_path / "index.scip", project_name="fixture")
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
    from oxn.scip.runner import run_indexer

    corpus = CORPUS.resolve()
    output = run_indexer(
        "python", corpus, tmp_path / "httpx.scip", project_name="httpx", project_version="0.28"
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
    from oxn.scip.runner import run_indexer

    corpus = CORPUS.resolve()
    index = run_indexer(
        "python", corpus, tmp_path / "httpx.scip", project_name="httpx", project_version="0.28"
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


# ---- dead code: vulture ---------------------------------------------------------------------


@pytest.mark.oracle
def test_dead_code_candidates_overlap_with_vulture(tmp_path) -> None:
    """Characterisation, not equality -- the two tools look for different things.

    vulture reports unused *variables, imports and attributes* as well as functions, and
    works per-file without a call graph. OXN reports entities unreachable from declared
    roots. Only unreached functions are comparable, and even there the two disagree about
    what counts as a root. The assertion is that OXN finds the obvious case.
    """
    vulture = pytest.importorskip("vulture")

    source = tmp_path / "m.py"
    source.write_text(
        "def used():\n    return 1\n\n\n"
        "def _never_called():\n    return 2\n\n\n"
        "def main():\n    return used()\n"
    )

    scanner = vulture.Vulture(verbose=False)
    scanner.scavenge([str(source)])
    theirs = {item.name for item in scanner.get_unused_code()}

    from oxn.graph.builder import build_file
    from oxn.languages import get_parser
    from oxn.metrics.callgraph import build_call_graph, default_roots, unreachable
    from oxn.profiles import get_profile

    profile = get_profile("python")
    data = source.read_bytes()
    tree = get_parser("python").parse(data)
    parsed = build_file("m.py", data, profile, tree.root_node)
    entities = {
        entity.id: (entity.qualified_name, "m.py", entity.kind.value)
        for entity in parsed.entities
        if entity.kind.value in {"function", "method", "class"}
    }
    graph = build_call_graph([])
    ours = {
        candidate.qualified_name.rsplit(".", 1)[-1]
        for candidate in unreachable(graph, entities, default_roots(entities))
    }

    assert "_never_called" in theirs, "vulture should flag the obvious case"
    assert "_never_called" in ours, "OXN should flag it too"
    assert "main" not in ours, "main is a declared root"


def _is_package_init_edge(source: str, target: str, edges: set[tuple[str, str]]) -> bool:
    """Does `source` also import a submodule of `target`?

    That is what makes the edge to the package itself real rather than spurious:
    `from oxn import thresholds` runs `oxn/__init__.py` on the way to `oxn.thresholds`.
    """
    return any(other.startswith(f"{target}.") for owner, other in edges if owner == source)
