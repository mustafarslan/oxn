"""`oxn review` -- the pull-request surface, driven from the installed script.

The newest user-facing command and, until this module, the one with no end-to-end
coverage at all: the lane drove `check`, `baseline`, `health`, `parse`, `serve` and the
report commands, and never `review`. That is the surface a CI job runs on someone else's
pull request, so "it works in the working tree" is the weakest place for it to be true.

Three properties here are P11's, and each is a claim OXN makes in prose elsewhere:

* **it never gates.** `oxn check` already does, and two gates disagreeing about one pull
  request is worse than one gate. So findings must come back on exit 0;
* **a finding knows where it came from.** `new`, `regression` and `baselined` are
  different facts about a pull request and collapsing them makes the comment useless;
* **the numbers come from OXN and the prose comes from a model.** `quotable` is the set a
  writer may cite, and it is the mechanism behind "a comment stating a number the
  measurement does not contain is refused rather than posted".
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from harness import FIXTURES, Installed, plant

pytestmark = pytest.mark.e2e


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=e2e@example.invalid", "-c", "user.name=e2e", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )


def _json(result) -> dict:
    return json.loads(result.stdout[result.stdout.index("{") :])


@pytest.fixture
def pull_request(installed: Installed, project: Path) -> Path:
    """A base branch, and a branch off it that adds one function over the ceiling.

    `project` already committed the tree, so `main` is the merge base and the new commit
    is what a pull request would carry.
    """
    _git(project, "branch", "-M", "main")
    _git(project, "checkout", "-q", "-b", "feature")
    plant(FIXTURES / "over_ceiling.py.txt", project / "src", name="big.py")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "add a function over the ceiling")
    return project


def test_review_reports_findings_without_gating(installed: Installed, pull_request: Path) -> None:
    """Exit 0 with a finding in hand. A review that blocks is a second gate."""
    result = installed.run("review", "--base", "main", "--json", cwd=pull_request)
    assert result.returncode == 0, (
        f"review gated on a finding; only `check` may do that: {result.stderr[-300:]}"
    )
    assert _json(result)["findings"], "review found nothing in a branch that adds a violation"


def test_a_finding_says_it_is_new_and_carries_its_trail(
    installed: Installed, pull_request: Path
) -> None:
    """`origin` distinguishes what the branch introduced from what it inherited."""
    finding = _json(installed.run("review", "--base", "main", "--json", cwd=pull_request))[
        "findings"
    ][0]
    assert finding["origin"] == "new", f"a function this branch added is {finding['origin']}"
    assert finding["introduced"] is True
    assert finding["value"] == 13.0 and finding["ceiling"] == 12.0
    assert (
        sum(int(step.split("+", 1)[1].split(" ", 1)[0]) for step in finding["explanation"])
        == finding["value"]
    ), "the increment trail does not add up to the score it explains"


def test_debt_the_branch_did_not_introduce_is_not_reported_as_new(
    installed: Installed, pull_request: Path
) -> None:
    """The distinction the comment lives on: inherited debt is not this author's doing."""
    assert installed.run("baseline", cwd=pull_request).returncode == 0
    _git(pull_request, "add", "-A")
    _git(pull_request, "commit", "-qm", "accept the debt")

    findings = _json(installed.run("review", "--base", "main", "--json", cwd=pull_request))[
        "findings"
    ]
    origins = {finding["origin"] for finding in findings}
    assert "new" not in origins, f"baselined debt is still being reported as new: {origins}"


def test_the_measurement_is_stamped_with_what_produced_it(
    installed: Installed, pull_request: Path
) -> None:
    """A number with no provenance cannot be rechecked later, which is what a stamp is for."""
    measured = _json(installed.run("review", "--base", "main", "--json", cwd=pull_request))[
        "measured"
    ]
    assert measured["oxn_version"], "no version in the stamp"
    assert len(measured["commit"]) == 40, f"not a full commit sha: {measured['commit']}"
    assert measured["ceilings"]["cognitive_complexity"] == 12.0
    assert measured["profiles"], "no language profile versions in the stamp"


def test_review_measures_the_range_rather_than_the_repository(
    installed: Installed, pull_request: Path
) -> None:
    """Three-dot semantics: the change against the merge base, not everything on `main`.

    The failure this guards is a review that walks the whole tree and blames a pull
    request for code it never touched.
    """
    report = _json(installed.run("review", "--base", "main", "--json", cwd=pull_request))
    assert report["files"]["changed"] == 1, f"changed {report['files']['changed']} files, not 1"
    assert report["files"]["measured"] == 1
    assert all(finding["path"] == "src/big.py" for finding in report["findings"])


def test_every_number_a_writer_may_quote_comes_from_the_measurement(
    installed: Installed, pull_request: Path
) -> None:
    """The honesty rule, checked as a set rather than trusted as a sentence.

    `quotable` is what stands between "a model writes the prose" and "a model invents the
    numbers". Every figure in the finding must be in it; a writer citing anything else is
    citing something OXN did not measure.
    """
    report = _json(installed.run("review", "--base", "main", "--json", cwd=pull_request))
    quotable = set(report["quotable"])
    assert quotable, "nothing is quotable, so the writer has no sourced numbers at all"

    finding = report["findings"][0]
    for number in (finding["value"], finding["ceiling"]):
        assert str(int(number)) in quotable, (
            f"{number} is in the finding and not quotable, so the writer cannot cite it"
        )


def test_init_github_writes_a_workflow_that_runs_review(
    installed: Installed, project: Path
) -> None:
    """`oxn init --github` is how the review surface reaches a repository at all."""
    assert installed.run("init", "--github", "--no-hook", "--no-mcp", cwd=project).returncode == 0
    workflows = list((project / ".github" / "workflows").glob("*.yml"))
    assert workflows, "`--github` wrote no workflow"
    body = "\n".join(path.read_text() for path in workflows)
    assert "oxn review" in body, "the workflow does not run `oxn review`"
    assert "issue_comment" in body, "the workflow is not triggered by a mention"


def test_a_review_of_nothing_is_not_a_failure(installed: Installed, project: Path) -> None:
    """A pull request that changed no measurable file must report cleanly, not crash.

    The empty range is the case a CI job hits constantly -- a docs-only change, a revert --
    and the one most likely to divide by zero.
    """
    result = installed.run("review", "--base", "HEAD", "--json", cwd=project)
    assert result.returncode == 0, result.stderr
    report = _json(result)
    assert report["findings"] == []
    assert report["files"]["changed"] == 0
