"""Cyclomatic and cognitive complexity against radon, lizard and complexipy.

radon and lizard contradict *each other*, so the documented-divergence table asserts all
three implementations exactly: if any tool changes behaviour, this fails and
docs/divergences.md gets revisited rather than quietly drifting."""

from __future__ import annotations

from pathlib import Path

import pytest

from oracle_support import CORPUS, _oxn_functions
from oxn.languages import get_parser
from oxn.metrics import cyclomatic_complexity, halstead
from oxn.profiles import get_profile

pytestmark = pytest.mark.oracle

radon_complexity = pytest.importorskip("radon.complexity")
radon_metrics = pytest.importorskip("radon.metrics")
radon_raw = pytest.importorskip("radon.raw")
complexipy = pytest.importorskip("complexipy")
lizard = pytest.importorskip("lizard")


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


def _complexipy_functions(path: Path) -> dict[str, int] | None:
    """complexipy's score per function name, or None when the oracle could not read the file.

    A broken oracle must not fail our suite -- it is evidence, not an authority -- so the
    file is skipped and the comparison simply has one fewer data point.
    """
    try:
        return {fn.name: fn.complexity for fn in complexipy.file_complexity(str(path)).functions}
    except Exception:  # noqa: BLE001 -- see the docstring
        return None


def _paired_scores(path: Path):
    """Functions *both* implementations scored, with both scores.

    Only the intersection is compared. complexipy reports things we do not treat as
    functions and vice versa, and counting a name only one side knows about would measure
    the difference in what each calls a function rather than how each scores one.
    """
    ours = _oxn_functions(path)
    theirs = _complexipy_functions(path)
    if ours is None or theirs is None:
        return
    for name, expected in theirs.items():
        if name in ours:
            yield name, ours[name], expected


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
        for name, ours, theirs in _paired_scores(path):
            compared += 1
            if ours == theirs:
                agreed += 1
            else:
                disagreements.append(f"{path.name}::{name} oxn={ours} complexipy={theirs}")

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


def test_sloc_excludes_docstrings_and_radon_agrees() -> None:
    """The defect: `docstrings_are_comments` was set, was read, and could not match.

    `_is_docstring` required the string's parent to be an `expression_statement`; the Python
    grammar OXN ships puts a docstring directly under its `block`. So every Python docstring
    counted as code, and `MAX_FUNCTION_SLOC` -- a gated ceiling -- charged this repository
    for documenting itself. radon is the oracle because it makes the same distinction with
    an entirely different implementation: it tokenizes, and reports docstring lines as
    `multi` rather than as `sloc`.

    The second case is the one that makes the fix non-trivial: a string in *statement*
    position is documentation, a string being assigned or passed is data, and widening the
    parent test must not lose that.
    """
    from oxn.metrics.size import line_counts

    documented = 'def f():\n    """One.\n\n    Two.\n    Three.\n    Four.\n    """\n    return 1\n'
    data = 'def g():\n    x = "one"\n    return {"k": "v"}\n'

    profile = get_profile("python")
    for source, expected in ((documented, 2), (data, 3)):
        root = get_parser("python").parse(source.encode()).root_node
        counts = line_counts(root.named_children[0], source.encode(), profile)
        assert counts.sloc == expected, f"{source!r} -> {counts}"
        assert counts.sloc == radon_raw.analyze(source).sloc, "radon must agree"

    assert (
        line_counts(
            get_parser("python").parse(documented.encode()).root_node.named_children[0],
            documented.encode(),
            profile,
        ).cloc
        == 6
    ), "and the docstring must be counted as comment, not discarded"
