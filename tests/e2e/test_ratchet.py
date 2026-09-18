"""The baseline ratchet, the unparseable rule, and the bounded repair loop.

These three are what make OXN adoptable on a repository that predates it, and each has a
failure mode that only shows up in sequence: a baseline that grows is an amnesty, a file
that cannot be parsed and can be baselined is a clean bill of health over a tree with that
file missing, and a repair loop with no bound is an agent editing forever.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from harness import FIXTURES, Installed, plant

pytestmark = pytest.mark.e2e


def _json(result) -> dict:
    return json.loads(result.stdout[result.stdout.index("{") :])


def _payload(path: Path, session: str) -> str:
    return json.dumps(
        {"tool_name": "Edit", "tool_input": {"file_path": str(path)}, "session_id": session}
    )


@pytest.fixture
def debt(installed: Installed, project: Path) -> Path:
    """A tree with one known violation, recorded as accepted debt."""
    plant(FIXTURES / "over_ceiling.py.txt", project / "src")
    assert installed.run("check", "--json", cwd=project).returncode == 2
    assert installed.run("baseline", cwd=project).returncode == 0
    return project


def test_recorded_debt_stops_failing_the_build(installed: Installed, debt: Path) -> None:
    result = installed.run("check", "--json", cwd=debt)
    assert result.returncode == 0, result.stdout
    report = _json(result)
    assert report["violations"] == []
    assert report["baselined"] == 1, "the violation was not recorded as debt"


def test_the_baseline_may_not_grow(installed: Installed, debt: Path) -> None:
    """Recording twice must record the same thing; a baseline that grows is an amnesty."""
    recorded = (debt / ".oxn" / "baseline.json").read_bytes()
    assert installed.run("baseline", cwd=debt).returncode == 0
    assert (debt / ".oxn" / "baseline.json").read_bytes() == recorded


def test_debt_that_gets_worse_fails_exactly_as_a_new_violation_does(
    installed: Installed, debt: Path
) -> None:
    """13 was accepted; 17 is not the debt that was accepted."""
    plant(FIXTURES / "worse_ceiling.py.txt", debt / "src", name="over_ceiling.py")

    result = installed.run("check", "--json", cwd=debt)

    assert result.returncode == 2, "a baselined violation getting worse must fail"
    regressions = _json(result)["regressions"]
    assert len(regressions) == 1, regressions
    assert regressions[0]["value"] == 17.0
    assert regressions[0]["entity"].endswith("over_ceiling.classify")


def test_repairing_the_debt_leaves_the_tree_clean(installed: Installed, debt: Path) -> None:
    """The other direction: a baselined violation that is fixed must not linger."""
    plant(FIXTURES / "same_shape" / "sample.py.txt", debt / "src", name="over_ceiling.py")
    result = installed.run("check", "--json", cwd=debt)
    assert result.returncode == 0, result.stdout
    assert _json(result)["violations"] == []


def test_a_file_that_does_not_parse_blocks_and_names_the_line(
    installed: Installed, project: Path
) -> None:
    """Silently dropping it would mean a clean report over a tree with the file missing."""
    plant(FIXTURES / "broken.py.txt", project / "src")

    result = installed.run("check", "--json", cwd=project)

    assert result.returncode == 2
    violations = _json(result)["violations"]
    assert [v["rule"] for v in violations] == ["unparseable"]
    assert violations[0]["line"] == 3, "the first error line was not named"


def test_an_unparseable_file_cannot_be_recorded_as_debt(
    installed: Installed, project: Path
) -> None:
    """Accepting it as debt is the clean-bill-of-health failure, written down.

    `baseline` succeeds -- there may be real debt in the same tree worth recording -- but it
    records *nothing* for this file, and the next check still blocks. Asserting on the exit
    code alone would pass against a baseline that quietly swallowed it.
    """
    plant(FIXTURES / "broken.py.txt", project / "src")

    assert installed.run("baseline", cwd=project).returncode == 0
    recorded = json.loads((project / ".oxn" / "baseline.json").read_text())
    assert recorded["violations"] == {}, "an unparseable file was accepted as debt"

    after = installed.run("check", "--json", cwd=project)
    assert after.returncode == 2, "baselining silenced a file OXN cannot parse"
    assert [v["rule"] for v in _json(after)["violations"]] == ["unparseable"]


def test_the_repair_loop_is_bounded_per_session(installed: Installed, project: Path) -> None:
    """Three failed repairs of the same violation, and the fourth answer says stop editing."""
    edited = plant(FIXTURES / "over_ceiling.py.txt", project / "src")
    counts = []
    for _ in range(4):
        result = installed.run("check", "--json", stdin=_payload(edited, "session-a"), cwd=project)
        assert result.returncode == 2
        counts.append(_json(result)["retry"]["attempts"])

    assert "retry budget spent" in result.stderr.lower(), result.stderr
    assert counts[0] != counts[-1], "the ledger never counted anything"
    ledger = project / ".oxn" / "cache" / "attempts" / "session-a.json"
    assert ledger.exists(), "the budget was enforced without a ledger to enforce it from"


def test_a_different_session_starts_with_a_fresh_budget(
    installed: Installed, project: Path
) -> None:
    """The count is per session: halting a *new* agent on the last one's debt is nonsense."""
    edited = plant(FIXTURES / "over_ceiling.py.txt", project / "src")
    for _ in range(4):
        installed.run("check", "--json", stdin=_payload(edited, "session-a"), cwd=project)

    result = installed.run("check", "--json", stdin=_payload(edited, "session-b"), cwd=project)

    assert result.returncode == 2
    assert "retry budget spent" not in result.stderr.lower()


def test_repairing_a_violation_forgets_its_attempts(installed: Installed, project: Path) -> None:
    """The count is per violation, and clearing one forgets it.

    Without this, an agent that spends its budget, fixes the function, and later breaks it
    again is refused a repair it has never actually been asked for. Exhaustion and
    per-session freshness both pass whether or not the ledger can forget, so this is the
    only leg that pins it.
    """
    edited = plant(FIXTURES / "over_ceiling.py.txt", project / "src")
    for _ in range(4):
        installed.run("check", "--json", stdin=_payload(edited, "session-c"), cwd=project)

    plant(FIXTURES / "same_shape" / "sample.py.txt", project / "src", name="over_ceiling.py")
    repaired = installed.run("check", "--json", stdin=_payload(edited, "session-c"), cwd=project)
    assert repaired.returncode == 0, repaired.stdout

    plant(FIXTURES / "over_ceiling.py.txt", project / "src")
    again = installed.run("check", "--json", stdin=_payload(edited, "session-c"), cwd=project)

    assert again.returncode == 2
    assert "retry budget spent" not in again.stderr.lower(), (
        "a repaired violation kept the attempts it was repaired after"
    )
