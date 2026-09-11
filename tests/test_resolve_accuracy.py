"""Grading L0/L1 against a SCIP oracle, without needing an indexer installed.

`test_oracles.py` measures this on real SCIP output, and that test is the authority --
but it needs `scip-python` on PATH and is skipped wherever that is absent, which is
everywhere by default. So the grading logic itself had no running coverage at all: a
refactor of it could not be verified by any test that actually executes.

These build a ScipDocument by hand instead. Small, exact, and they run everywhere.
"""

from __future__ import annotations

from oxn.graph.builder import build_file
from oxn.languages import get_parser
from oxn.profiles import get_profile
from oxn.resolve.measure import ResolutionAccuracy, _Grading, grade_file
from oxn.resolve.scopes import build_scopes
from oxn.resolve.symbols import build_project_symbols
from oxn.scip.index import ScipDocument, ScipOccurrence

SOURCE = """
def helper(value):
    return value


def caller(value):
    return helper(value)
"""


def _graded(symbol: str, *, line: int, char: int, truth_is_helper: bool = True):
    """Grade `SOURCE`'s single call site against one hand-made SCIP occurrence."""
    profile = get_profile("python")
    data = SOURCE.encode()
    tree = get_parser(profile.grammar).parse(data)
    parsed = build_file("m.py", data, profile, tree.root_node)
    entities = list(parsed.entities)

    symbols = build_project_symbols(
        {"m.py": entities}, {"m.py": build_scopes(tree.root_node, profile)}
    )
    helper = next(e for e in entities if e.name == "helper")
    document = ScipDocument(
        relative_path="m.py",
        occurrences=[ScipOccurrence(symbol, line, char, line, char + 6, 0)],
    )
    accuracy = ResolutionAccuracy(language="python")
    grade_file(
        _Grading(
            "m.py",
            profile,
            document,
            symbols,
            {symbol: helper.id if truth_is_helper else "someone-else"},
            accuracy,
        ),
        tree.root_node,
    )
    return accuracy


#: `helper(value)` sits on line 7 (0-based 6), starting at column 11.
CALL_LINE, CALL_CHAR = 6, 11


def test_a_correct_guess_is_counted_correct() -> None:
    accuracy = _graded("`m`/helper().", line=CALL_LINE, char=CALL_CHAR)
    assert accuracy.graded_call_sites == 1
    assert accuracy.answered == 1
    assert accuracy.correct == 1
    assert not accuracy.disagreements


def test_a_wrong_guess_is_recorded_with_its_evidence() -> None:
    """A disagreement must name the file, line and both answers, or it cannot be chased."""
    accuracy = _graded("`m`/helper().", line=CALL_LINE, char=CALL_CHAR, truth_is_helper=False)
    assert accuracy.answered == 1
    assert accuracy.correct == 0
    assert len(accuracy.disagreements) == 1
    assert "m.py:7" in accuracy.disagreements[0]
    assert "helper()" in accuracy.disagreements[0]


def test_an_oracle_that_contradicts_the_source_is_excluded_not_graded() -> None:
    """30.5% of httpx call sites hit this: scip-python names an adjacent symbol.

    Grading against a ground truth that is wrong one time in five measures the oracle
    rather than us, so those sites are excluded and counted separately.
    """
    accuracy = _graded("`m`/somethingelse().", line=CALL_LINE, char=CALL_CHAR)
    assert accuracy.excluded_untrustworthy == 1
    assert accuracy.graded_call_sites == 0


def test_a_call_site_scip_did_not_place_is_skipped_silently() -> None:
    """No occurrence at that position means there is nothing to grade against."""
    accuracy = _graded("`m`/helper().", line=99, char=0)
    assert accuracy.graded_call_sites == 0
    assert accuracy.excluded_untrustworthy == 0
    assert not accuracy.disagreements


def test_step_two_answers_from_the_file_the_name_came_from() -> None:
    """**"Any file this one imports" is a guess dressed as evidence.**

    Step 2 asked whether *any* imported file declares the name and took the first in sorted
    order -- so a file importing two modules that both declare `helper` answered confidently
    from whichever sorted first. The import table already knows better: `aliases` records which
    specifier bound the name and `specifier_targets` records where that specifier was placed.

    On `javascript-eslint` this fires for 1,950 of 1,965 step-2 answers, so it is the common
    path rather than a corner.

    **Measured on all six languages, and Go is why it is worth having.** Python, Rust,
    TypeScript, JavaScript and Java are identical with it and without -- same graded count,
    same confident count, same precision. Go is not: confident precision **99.116% -> 99.411%**
    with the confident count unchanged at 1,018, so it converts wrong answers into right ones
    rather than adding answers. That is package-qualified dispatch, `metrics.NewCounter()`,
    where "any file this one imports" and "the file `metrics` was imported from" are different
    files and only the second is evidence. An earlier claim here said it moved no published
    number; that was measured on eslint alone, before Go's indexer was installed.
    """
    from oxn.graph.model import Entity, EntityKind
    from oxn.resolve.symbols import ProjectSymbols

    def declared(name: str, path: str) -> Entity:
        return Entity(
            id=f"{path}::{name}",
            kind=EntityKind.FUNCTION,
            name=name,
            qualified_name=f"{path}.{name}",
            file_path=path,
            start_byte=0,
            end_byte=1,
            start_line=1,
            end_line=1,
        )

    symbols = ProjectSymbols()
    symbols.by_file = {
        "a/first.js": {"helper": [declared("helper", "a/first.js")]},
        "b/second.js": {"helper": [declared("helper", "b/second.js")]},
    }
    symbols.imports = {"caller.js": {"a/first.js", "b/second.js"}}
    symbols.aliases = {"caller.js": {"helper": "./b/second"}}
    symbols.specifier_targets = {"caller.js": {"./b/second": ("b/second.js",)}}

    found = symbols.resolve_call("caller.js", "helper")

    assert found is not None
    assert found.file_path == "b/second.js", "the file the name was imported from, not the first"
    assert found.confidence == 1.0
