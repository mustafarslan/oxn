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
