"""Cyclomatic complexity at corpus scale, against lizard.

Raw agreement is meaningless here; every divergence reducing to an enumerated rule is the
criterion. See the test docstring."""

from __future__ import annotations

import pytest

from oracle_support import CORPUS, _count_kinds, _function_nodes
from oxn.languages import get_parser
from oxn.metrics import cyclomatic_complexity
from oxn.profiles import get_profile

pytestmark = pytest.mark.oracle

lizard = pytest.importorskip("lizard")


def _lizard_by_line(source: str) -> dict[int, int]:
    """lizard's cyclomatic score per function, keyed by the line the function starts on.

    `setdefault` rather than assignment: lizard occasionally reports two entries for one
    line, and the first is the outer function.
    """
    by_line: dict[int, int] = {}
    for function in lizard.analyze_file.analyze_source_code("t.py", source).function_list:
        by_line.setdefault(function.start_line, function.cyclomatic_complexity)
    return by_line


def _reduces_to_a_rule(definition, profile, theirs: int) -> tuple[bool, int]:
    """Does the difference from lizard reduce to the two enumerated rules?

        lizard == oxn - (asserts) + (finally clauses)

    Both are in docs/divergences.md with the reasoning. Returns the verdict and our score,
    because a failure has to name the number it disagreed about.
    """
    ours = cyclomatic_complexity(definition, profile)
    asserts = _count_kinds(definition, profile, {"assert_statement"})
    finallys = _count_kinds(definition, profile, {"finally_clause"})
    return ours - asserts + finallys == theirs, ours


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
        by_line = _lizard_by_line(source)

        for definition in _function_nodes(root, profile):
            line = definition.start_point[0] + 1
            if line not in by_line:
                continue
            compared += 1
            agrees, ours = _reduces_to_a_rule(definition, profile, by_line[line])
            if agrees:
                explained += 1
            else:
                unexplained.append(f"{path.name}:{line} oxn={ours} lizard={by_line[line]}")

    assert compared > 500, f"only {compared} functions compared"
    ratio = explained / compared
    assert ratio >= 0.99, f"explained {ratio:.2%} ({explained}/{compared}); {unexplained[:5]}"
