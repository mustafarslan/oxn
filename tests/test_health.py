"""The informational output: risk profiles that name the code they are about.

P10's exit criterion asks for a score that is "stable and explainable -- every point traces
back to named entities". These tests hold both halves of that, and one thing it is *not*:

* every share is accompanied by the entities carrying it, because a bare number is the output
  `docs/metrics.md` section 10.1 refuses -- "an agent cannot act on `your score is 71`";
* the same code measures the same twice, since a trend line over a number that moves on its
  own is worse than no trend line;
* and it never blocks. `check.py` decides; this ranks.

There is deliberately no composite score and no 1-5 rating. Section 10.2's recipe ends by
mapping a profile to a rating "via calibrated boundaries", and SIG derived those from roughly
a hundred systems; OXN has five, and they are the same five its ceilings were measured
against, so a rating fitted to them would say how httpx compares to httpx.
"""

from __future__ import annotations

from pathlib import Path

import pytest

OVER = '''
def wide(request):
    """Cognitive complexity well past any ceiling."""
    total = 0
    for a in range(10):
        if a > 1:
            for b in range(10):
                if b > 2:
                    while total < 5:
                        if request:
                            total += 1
                        else:
                            total -= 1
    return total
'''

FINE = '''
def narrow(request):
    """Nothing here troubles a ceiling."""
    return 1 if request else 0
'''


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "over.py").write_text(OVER)
    (tmp_path / "fine.py").write_text(FINE)
    return tmp_path


def _health(paths: list[str] | None = None) -> dict:
    from oxn.health import run_health

    return run_health(paths or ["."])


def test_every_share_names_the_entities_that_carry_it(project: Path) -> None:
    """The explainability half of the exit criterion, asserted rather than asserted-in-prose."""
    profiles = _health()["profiles"]
    cognitive = profiles["cognitive_complexity"]

    assert cognitive["share_over_ceiling"] > 0, "the fixture is over the ceiling by construction"
    assert cognitive["worst"], "a share with no entities attached is the number 10.1 refuses"
    worst = cognitive["worst"][0]
    assert worst["entity"].endswith("wide")
    assert worst["path"] == "over.py" and worst["line"] > 0
    assert worst["value"] > cognitive["ceiling"]


def test_the_share_is_the_line_ratio_and_not_the_entity_ratio(project: Path) -> None:
    """Section 10.2's reason: these distributions are heavy-tailed.

    Asserted against the *value* rather than against "the two differ". Any two functions of
    unequal length make a count ratio and a line ratio differ, so an inequality would pass for
    a weighting by `lines` instead of `sloc`, or by anything else monotonic in size.
    """
    cognitive = _health()["profiles"]["cognitive_complexity"]
    worst = cognitive["worst"][0]

    expected = worst["sloc"] / cognitive["sloc"]
    assert cognitive["share_over_ceiling"] == pytest.approx(expected), (
        f"share {cognitive['share_over_ceiling']} is not the SLOC ratio {expected}"
    )
    by_entity = cognitive["entities_over"] / cognitive["entities"]
    assert cognitive["share_over_ceiling"] != pytest.approx(by_entity)


def test_a_clean_tree_reports_zero_rather_than_nothing(project: Path) -> None:
    """Absence and zero must not look alike -- `docs/metrics.md` section 4.1's rule."""
    (project / "over.py").unlink()
    profiles = _health()["profiles"]
    assert profiles["cognitive_complexity"]["share_over_ceiling"] == 0.0
    assert profiles["cognitive_complexity"]["entities"] > 0, "the clean file is still counted"
    assert profiles["cognitive_complexity"]["worst"] == []


def test_the_same_code_measures_the_same_twice(project: Path) -> None:
    """A trend line over a number that moves on its own is worse than no trend line."""
    assert _health() == _health()


def test_health_reports_every_declared_ceiling_except_the_derived_one(project: Path) -> None:
    """A ceiling the health view silently drops is a blind spot; `shredding` is not one.

    `shredding` shares the cognitive ceiling and carries its metric only on cluster roots, so
    its denominator would be *shred-root lines* where every other profile's is all callable
    lines. Printing that in the same column would be a share of a different population. It is
    excluded on purpose, and this test fails if a second `follows` rule appears and nobody
    thinks about which denominator it wants.
    """
    from oxn.config import GATED_METRICS, Config

    settings = Config.load()
    derived = {rule for rule in settings.ceilings if GATED_METRICS[rule].follows}
    assert derived == {"shredding"}, f"a new derived rule needs a decision here: {derived}"
    assert set(_health()["profiles"]) == set(settings.ceilings) - derived


def test_health_never_blocks(project: Path) -> None:
    """It returns a payload and no exit code: deciding is `check.py`'s job, not this one's."""
    payload = _health()
    assert payload["status"] == "OK", "a tree with a violation in it is still OK to report on"
    assert "exit_code" not in payload and "violations" not in payload
