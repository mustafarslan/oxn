"""Tier 1.5: duplication, verbosity and structural erosion."""

from __future__ import annotations

import pytest

from oxn.languages import get_parser
from oxn.profiles import get_profile
from oxn.volume.clones import file_windows, find_clones
from oxn.volume.erosion import erosion_at, gini, structural_erosion
from oxn.volume.tokens import normalized_tokens

BLOCK = """
    total = 0
    for item in collection:
        if item.enabled and item.value > threshold:
            total += item.value * weight
        elif item.fallback is not None:
            total += item.fallback
        else:
            total += 0
    return total
"""
RENAMED = BLOCK.replace("total", "sum_").replace("item", "entry").replace("collection", "items")


def detect(sources: dict[str, str], *, min_tokens: int = 30, min_lines: int = 4):
    profile = get_profile("python")
    windows = {}
    for path, text in sources.items():
        root = get_parser("python").parse(text.encode()).root_node
        windows[path] = file_windows(path, root, profile, min_tokens=min_tokens)
    return find_clones(windows, min_tokens=min_tokens, min_lines=min_lines)


# ---- normalization ---------------------------------------------------------------------


def test_identifiers_and_literals_are_erased() -> None:
    """Type-2 detection depends on this: renaming must not hide a copy."""
    profile = get_profile("python")
    root = get_parser("python").parse(b"def f(a):\n    return a + 1\n").root_node
    normalized = [token.normalized for token in normalized_tokens(root, profile)]
    assert normalized == ["def", "$ID", "(", "$ID", "return", "$ID", "+", "$LIT"]


def test_renaming_produces_an_identical_token_stream() -> None:
    profile = get_profile("python")
    parse = lambda text: [  # noqa: E731
        t.normalized for t in normalized_tokens(get_parser("python").parse(text).root_node, profile)
    ]
    assert parse(b"def f(alpha):\n    return alpha\n") == parse(b"def g(beta):\n    return beta\n")


# ---- clone detection -------------------------------------------------------------------


def test_finds_a_renamed_copy_within_one_file() -> None:
    source = (
        f"def first(collection, threshold, weight):{BLOCK}\n"
        f"def second(items, threshold, weight):{RENAMED}"
    )
    report = detect({"a.py": source})
    assert len(report.clone_classes) == 1
    assert len(report.clone_classes[0].occurrences) == 2
    assert report.duplication_ratio > 0.4


def test_finds_a_copy_across_files() -> None:
    report = detect(
        {
            "a.py": f"def first(collection, threshold, weight):{BLOCK}",
            "b.py": f"def second(items, threshold, weight):{RENAMED}",
        }
    )
    assert len(report.clone_classes) == 1
    assert {clone.path for clone in report.clone_classes[0].occurrences} == {"a.py", "b.py"}


@pytest.mark.parametrize(
    "arrangement",
    [
        "{a}\n{b}",
        "{a}\n{b}\ndef trailing(x):\n    return x + 1\n",
        "def leading(q):\n    return q\n{a}\n{b}",
        "{a}\ndef between(q):\n    return q\n{b}",
    ],
)
def test_surrounding_code_does_not_hide_the_clone(arrangement: str) -> None:
    """Extension must stop at the clone's real end.

    After normalization a following ``def f(...)`` header looks identical to the start of
    the copy, so unbounded extension runs past the boundary until the occurrences overlap
    and the clone is discarded. This arrangement matrix is what caught that.
    """
    source = arrangement.format(
        a=f"def first(collection, threshold, weight):{BLOCK}",
        b=f"def second(items, threshold, weight):{RENAMED}",
    )
    report = detect({"a.py": source})
    assert len(report.clone_classes) == 1, "the clone was lost"


def test_distinct_code_is_not_reported() -> None:
    report = detect(
        {
            "a.py": "def first(x):\n    return x + 1\n",
            "b.py": "def second(y):\n    while y:\n        y -= 1\n    return y\n",
        }
    )
    assert report.clone_classes == []
    assert report.duplication_ratio == 0.0


def test_short_repeats_are_below_the_threshold() -> None:
    """Idiomatic boilerplate must not register as duplication."""
    source = "def a():\n    return 1\n\ndef b():\n    return 1\n\ndef c():\n    return 1\n"
    assert detect({"a.py": source}, min_tokens=50).clone_classes == []


def test_window_hashes_are_stable_across_processes() -> None:
    """`hash()` is salted per process; window hashes must not be, or the cache never hits."""
    import json
    import subprocess
    import sys

    script = (
        "import json;"
        "from oxn.languages import get_parser;"
        "from oxn.profiles import get_profile;"
        "from oxn.volume.clones import file_windows;"
        "p=get_profile('python');"
        "src=open('src/oxn/volume/clones.py','rb').read();"
        "_,b=file_windows('c.py', get_parser('python').parse(src).root_node, p);"
        "print(json.dumps(sorted(b)[:5]))"
    )
    runs = [
        json.loads(
            subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, check=True
            ).stdout
        )
        for _ in range(2)
    ]
    assert runs[0] == runs[1]


def test_duplicate_lines_are_attributed_per_file() -> None:
    report = detect(
        {
            "a.py": f"def first(collection, threshold, weight):{BLOCK}",
            "b.py": f"def second(items, threshold, weight):{RENAMED}",
        }
    )
    lines = report.duplicate_lines_by_file()
    assert set(lines) == {"a.py", "b.py"}
    assert all(len(spans) > 4 for spans in lines.values())


# ---- erosion ---------------------------------------------------------------------------


def test_gini_is_zero_for_a_uniform_distribution() -> None:
    assert gini([5] * 20) == pytest.approx(0.0, abs=1e-9)


def test_gini_rises_as_complexity_concentrates() -> None:
    spread = gini([10, 10, 10, 10, 10])
    concentrated = gini([1, 1, 1, 1, 46])
    assert 0 <= spread < concentrated < 1


def test_gini_handles_degenerate_inputs() -> None:
    assert gini([]) == 0.0
    assert gini([0, 0, 0]) == 0.0
    assert 0.0 <= gini([7]) < 1.0


def test_erosion_at_k_of_a_uniform_distribution_is_k() -> None:
    assert erosion_at([5] * 20, 0.10) == pytest.approx(0.10)


def test_erosion_rises_when_mass_moves_to_the_worst_function() -> None:
    before = erosion_at([10, 10, 10, 10, 10, 10, 10, 10, 10, 10])
    after = erosion_at([1, 1, 1, 1, 1, 1, 1, 1, 1, 91])
    assert after > before


def test_erosion_report_ranks_the_worst_functions() -> None:
    report = structural_erosion({"a": 1.0, "b": 2.0, "c": 30.0})
    assert report.function_count == 3
    assert report.worst[0] == ("c", 30.0)
    assert report.erosion_at_k == pytest.approx(30 / 33)
    assert report.as_dict()["gini"] > 0
