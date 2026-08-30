"""Oracle-free invariants.

These run on every push and need no third-party tool. They catch a whole class of error
that golden files miss: a metric that is wrong in a way that is *consistently* wrong.
"""

from __future__ import annotations

import re

import pytest

from oxn.languages import get_parser
from oxn.metrics import (
    cognitive_complexity,
    cyclomatic_complexity,
    exit_points,
    halstead,
    line_counts,
    max_nesting_depth,
)
from oxn.profiles import get_profile

SAMPLES = [
    "def f():\n    pass\n",
    "def f(a):\n    if a: pass\n",
    "def f(a,b):\n    if a and b:\n        for i in a:\n            if b: pass\n",
    "def f(a):\n    try:\n        return [x for x in a if x]\n    except E:\n        raise\n",
    "def f(a):\n    return f(a-1) if a else 0\n",
]


def rename(source: str) -> str:
    """Consistently rename identifiers, on word boundaries.

    Naive substring replacement corrupts keywords -- ``pass`` becomes ``palphass`` and
    ``and`` becomes ``alphand`` -- which makes the invariant untestable.
    """
    for old, new in (("a", "alpha"), ("b", "beta")):
        source = re.sub(rf"\b{old}\b", new, source)
    return source


def parse(language: str, source: str):
    profile = get_profile(language)
    root = get_parser(language).parse(source.encode()).root_node
    return profile, profile.unwrap(root.named_children[0])


@pytest.mark.parametrize("source", SAMPLES)
def test_cyclomatic_is_at_least_one(source: str) -> None:
    profile, fn = parse("python", source)
    assert cyclomatic_complexity(fn, profile) >= 1


@pytest.mark.parametrize("source", SAMPLES)
def test_metrics_are_invariant_under_renaming(source: str) -> None:
    """Consistent renaming changes no structural metric."""
    renamed = rename(source)
    profile, original = parse("python", source)
    _, other = parse("python", renamed)
    assert cyclomatic_complexity(original, profile) == cyclomatic_complexity(other, profile)
    assert (
        cognitive_complexity(original, profile).score == cognitive_complexity(other, profile).score
    )
    assert max_nesting_depth(original, profile) == max_nesting_depth(other, profile)


@pytest.mark.parametrize("source", SAMPLES)
def test_metrics_are_invariant_under_comments(source: str) -> None:
    commented = source.replace("\n", "  # note\n", 1)
    profile, original = parse("python", source)
    _, other = parse("python", commented)
    assert cyclomatic_complexity(original, profile) == cyclomatic_complexity(other, profile)
    assert (
        cognitive_complexity(original, profile).score == cognitive_complexity(other, profile).score
    )


@pytest.mark.parametrize("count", [1, 2, 3, 5, 8])
def test_a_run_of_like_operators_scores_exactly_one(count: int) -> None:
    """The boolean-sequence rule: one increment per run, however long the run."""
    condition = " and ".join("abcdefghij"[:count])
    profile, fn = parse(
        "python", f"def f({', '.join('abcdefghij'[:count])}):\n    if {condition}: pass\n"
    )
    # +1 for the `if`, +1 for the single run (a lone operand is no run at all)
    assert cognitive_complexity(fn, profile).score == (2 if count > 1 else 1)


@pytest.mark.parametrize("count", [2, 3, 4, 6])
def test_alternating_operators_score_one_per_run(count: int) -> None:
    names = "abcdefghij"[:count]
    condition = names[0]
    for index, name in enumerate(names[1:]):
        condition += f" {'and' if index % 2 == 0 else 'or'} {name}"
    profile, fn = parse("python", f"def f({', '.join(names)}):\n    if {condition}: pass\n")
    expected_runs = count - 1
    assert cognitive_complexity(fn, profile).score == 1 + expected_runs


def test_wrapping_in_an_if_adds_exactly_one_plus_the_nested_structures() -> None:
    """Nesting is what separates cognitive complexity from cyclomatic complexity."""
    profile, flat = parse("python", "def f(a):\n    for i in a:\n        if a: pass\n")
    _, wrapped = parse(
        "python", "def f(a):\n    if a:\n        for i in a:\n            if a: pass\n"
    )
    before = cognitive_complexity(flat, profile).score  # for(1) + if(2) = 3
    after = cognitive_complexity(wrapped, profile).score  # if(1) + for(2) + if(3) = 6
    assert before == 3
    assert after == 6
    # Cyclomatic, by contrast, is unmoved by nesting -- it only gains the new `if`.
    assert cyclomatic_complexity(wrapped, profile) == cyclomatic_complexity(flat, profile) + 1


@pytest.mark.parametrize("source", SAMPLES)
def test_halstead_length_is_the_sum_of_its_parts(source: str) -> None:
    profile, fn = parse("python", source)
    measures = halstead(fn, profile)
    assert measures.length == measures.total_operators + measures.total_operands
    assert measures.vocabulary == measures.distinct_operators + measures.distinct_operands
    assert measures.distinct_operators <= measures.total_operators
    assert measures.distinct_operands <= measures.total_operands
    assert measures.volume >= 0


@pytest.mark.parametrize("source", SAMPLES)
def test_halstead_counts_survive_renaming(source: str) -> None:
    """Renaming changes which operands exist, never how many."""
    profile, original = parse("python", source)
    _, renamed = parse("python", rename(source))
    first, second = halstead(original, profile), halstead(renamed, profile)
    assert (first.distinct_operands, first.total_operands) == (
        second.distinct_operands,
        second.total_operands,
    )


def test_nesting_depth_matches_the_shape_of_the_code() -> None:
    profile, fn = parse(
        "python",
        "def f(a):\n    if a:\n        for i in a:\n            while i:\n                pass\n",
    )
    assert max_nesting_depth(fn, profile) == 3


def test_exit_points_counts_returns_and_raises() -> None:
    profile, fn = parse(
        "python",
        "def f(a):\n    if a:\n        return 1\n    if a:\n        raise E\n    return 0\n",
    )
    assert exit_points(fn, profile) == 3


def test_line_counts_partition_the_span() -> None:
    source = "def f(a):\n    # comment\n\n    return a\n"
    profile, fn = parse("python", source)
    counts = line_counts(fn, source.encode(), profile)
    assert counts.lines == 4
    assert counts.cloc == 1
    assert counts.blank == 1
    assert counts.sloc == 2
    assert 0.0 <= counts.comment_density <= 1.0


def test_nested_functions_do_not_inflate_the_parent_cyclomatic_score() -> None:
    """A nested definition is its own entity; a parent's number stays about the parent."""
    profile, fn = parse(
        "python",
        "def f(a):\n    def g(b):\n        if b: pass\n        if b: pass\n    if a: pass\n",
    )
    assert cyclomatic_complexity(fn, profile) == 2
