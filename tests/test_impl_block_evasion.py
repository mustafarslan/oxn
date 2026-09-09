"""A Rust type's methods, and the block boundary that used to hide half of them.

`methods_per_class` and `weighted_methods_per_class` were added to catch the God Class, and
in Rust they counted per ``impl`` block rather than per type. A block is not a class -- it is
a fragment of one, and a type may have any number of them -- so fourteen methods split seven
and seven walked through a ceiling of twelve that the identical fourteen failed in one block.

That is a cheaper evasion than any this project has tested. `shredding`'s escapes need a
renaming or a second caller; this one needs a newline and the word ``impl``, and the result
is *more* idiomatic Rust than the version that fails, not less. It was found by asking the
two paths that both compute NOM to agree: `oxn classes` read 14 through
`resolve.members.implemented_type` while the gate read 7, and two answers to one question
means the smaller is the one a ceiling gets compared against.

The trait row matters for the same reason: `impl Show for Wide` names `Wide` in the field
this reads, so a type's inherent and trait methods total together. Splitting a God Class into
a trait impl is otherwise the same evasion with an extra step.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oxn.thresholds import MAX_METHODS_PER_CLASS

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "impl_blocks"

#: All three carry the same fourteen methods on `Wide`, arranged differently.
OVER = ("one_block", "two_blocks", "trait_impl")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _check(project: Path, variant: str) -> tuple[int, set[str], dict[str, float]]:
    from oxn.check import run_check

    (project / f"{variant}.rs").write_text((FIXTURES / f"{variant}.rs.txt").read_text())
    report = run_check([f"{variant}.rs"], use_baseline=False)
    return (
        report.exit_code,
        {finding.rule for finding in report.blocking},
        {finding.rule: finding.value for finding in report.blocking},
    )


@pytest.mark.parametrize("variant", OVER)
def test_a_type_is_counted_whole_however_its_blocks_are_arranged(
    project: Path, variant: str
) -> None:
    """The evasion and its control, held to the same number rather than only to "blocked"."""
    exit_code, rules, values = _check(project, variant)
    assert exit_code != 0, f"{variant}: fourteen methods on one type passed the gate"
    assert "methods_per_class" in rules, sorted(rules)
    assert values["methods_per_class"] == 14, f"{variant}: counted {values['methods_per_class']}"


def test_the_count_is_reported_against_the_type_not_the_block(project: Path) -> None:
    """`impl` blocks are unnamed, and an unnamed offender is one a reader cannot act on."""
    from oxn.check import run_check

    (project / "two_blocks.rs").write_text((FIXTURES / "two_blocks.rs.txt").read_text())
    report = run_check(["two_blocks.rs"], use_baseline=False)
    offenders = {finding.entity for finding in report.blocking}
    assert any(name.endswith("Wide") for name in offenders), sorted(offenders)


def test_a_type_under_the_ceiling_still_passes(project: Path) -> None:
    """The false-positive side: totalling blocks must not make every split type a violation.

    Eight methods over two blocks is a legitimate arrangement and stays well under twelve.
    Without this the test above would pass for a gate that simply rejected every `impl`.
    """
    exit_code, rules, _ = _check(project, "under")
    assert exit_code == 0, f"a type with 8 methods was blocked: {sorted(rules)}"
    assert MAX_METHODS_PER_CLASS > 8, "the fixture must sit under the ceiling to prove anything"
