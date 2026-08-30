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
    ("def f(a):\n    match a:\n        case 1: pass\n        case _: pass\n", 2, 3, 3),
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
