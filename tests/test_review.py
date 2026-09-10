"""`oxn review`, and the rule that the prose may not invent a number.

The measurement half is `oxn check` with a different set of paths, so it is tested lightly
here and thoroughly there. What is new is the refusal: OXN produces the numbers, a model
produces the sentences, and a sentence containing a number OXN did not measure is not posted
at all. That is ADR-0002's line between what may block and what may only inform, applied to a
comment -- and unlike most applications of it, this one is mechanical and can be asserted.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from oxn.review import changed_files, numeral, quotable_numbers, run_review, unquotable
from oxn.writers import FakeWriter, ReviewComment, write_review

STAMP = {"commit": "a" * 40, "oxn_version": "0.1.0", "profiles": {}, "ceilings": {}}

OVER_CEILING = "def wide(a, b, c, d, e, f):\n    return a + b + c + d + e + f\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """`main` with one commit, and a `feature` branch carrying the pull request."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "oxn.yaml").write_text(
        "ceilings:\n  parameter_count: 5\nexclude:\n  - 'vendor/*'\n"
    )
    (tmp_path / "kept.py").write_text("def small():\n    return 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")

    # A branch, because `base...head` is the diff against the *merge base*: with both commits
    # on `main` the merge base is `HEAD` itself and the range is correctly empty.
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    (tmp_path / "added.py").write_text(OVER_CEILING)
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "big.py").write_text(OVER_CEILING)
    (tmp_path / "notes.md").write_text("prose\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "head")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_the_range_is_measured_and_the_excluded_file_is_not(repo: Path) -> None:
    """Three files changed; one is Markdown and one is vendored, and neither is ours.

    `git` has never read `oxn.yaml`, so the exclusion has to be applied to its output as well
    as to the walk -- the same defect `oxn index` had, where two commands disagreed about
    which files belong to the project.
    """
    payload = run_review("main", "HEAD")

    assert payload["files"] == {"changed": 3, "measured": 1, "excluded": 2}
    assert payload["status"] == "FINDINGS"
    found = [row for row in payload["findings"] if row["origin"] == "new"]
    assert [row["rule"] for row in found] == ["parameter_count"]
    assert found[0]["path"] == "added.py"
    assert "vendor/big.py" not in {row["path"] for row in payload["findings"]}


def test_a_finding_says_where_it_stands_with_the_baseline(repo: Path) -> None:
    """`origin` per finding, because a review that opens with debt the author did not create
    is the false positive that ends adoption."""
    (repo / ".oxn").mkdir(exist_ok=True)
    (repo / ".oxn" / "baseline.json").write_text(
        '{"violations": {"parameter_count|added.py|added.wide": 6.0}}'
    )
    payload = run_review("main", "HEAD")

    assert payload["counts"] == {"new": 0, "regression": 0, "baselined": 1}
    assert payload["findings"][0]["origin"] == "baselined"
    assert payload["status"] == "OK", "pre-existing debt is not something to action"


def test_the_measurement_says_what_it_measured(repo: Path) -> None:
    """A pull request gets pushed to, and a stale comment reads like a current one.

    ADR-0004 makes the same argument for the MCP server: an answer that cannot be recognised
    as stale is worse than no answer.
    """
    payload = run_review("main", "HEAD")
    stamp = payload["measured"]

    assert len(stamp["commit"]) == 40, stamp
    assert stamp["profiles"]["python"] > 0
    assert stamp["ceilings"]["parameter_count"] == 5


def test_a_deleted_file_is_not_measured(repo: Path) -> None:
    """`git diff` names it and there is nothing left to open."""
    (repo / "kept.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "delete")

    assert "kept.py" not in changed_files(repo, "main", "HEAD")


# ---- the honesty rule ----------------------------------------------------------------------


PAYLOAD = {
    "status": "FINDINGS",
    "files": {"changed": 3, "measured": 1, "excluded": 2},
    "counts": {"new": 1, "regression": 0, "baselined": 0},
    "findings": [
        {
            "rule": "parameter_count",
            "path": "added.py",
            "line": 1,
            "value": 6.0,
            "ceiling": 5.0,
            "origin": "new",
            "message": "added.wide has parameter_count 6, above the ceiling of 5",
        }
    ],
}


def test_a_comment_quoting_only_measured_numbers_is_posted() -> None:
    writer = FakeWriter(replies=["`added.py:1` — `parameter_count` is 6, above the ceiling of 5."])
    comment = write_review(PAYLOAD, writer)

    assert not comment.refused, comment
    assert comment.attempts == 1
    assert comment.model == "fake"


def test_a_comment_that_invents_a_number_is_refused_rather_than_posted() -> None:
    """**The rule.** 20% is not in the measurement, and neither is anything it was computed
    from -- a model that can do arithmetic on OXN's numbers can do it wrongly, and a reader
    who sees prose does not audit it."""
    writer = FakeWriter(
        replies=[
            "This raises parameter_count by 20% across the diff.",
            "Still 20% worse than before.",
        ]
    )
    comment = write_review(PAYLOAD, writer)

    assert comment.refused, comment
    assert comment.invented == ("20",)
    assert comment.attempts == 2
    assert comment.body == "", "a refused comment is silence, not a warning"


def test_an_incidental_numeral_is_refused_too_and_that_is_deliberate() -> None:
    """ "Python 3.11" is not dishonest and is still refused.

    The alternative is matching each number only against the metric named beside it, which is
    a parser that has to understand English. A review tool that is subtly wrong about its own
    honesty rule is worse than one that occasionally asks for a sentence to be rewritten, and
    the system prompt tells the writer not to introduce numerals for this reason.
    """
    writer = FakeWriter(replies=["Measured on Python 3.11.", "Measured on Python 3.11."])
    comment = write_review(PAYLOAD, writer)

    assert comment.refused
    assert comment.invented == ("3.11",)


def test_the_second_attempt_is_shown_the_numbers_it_invented() -> None:
    """One arithmetic aside in an otherwise accurate paragraph is worth asking about again.

    There is no third attempt: a model that keeps inventing numbers after being shown them is
    not going to stop, and a tool whose honesty depends on how many times it asked is not
    honest.
    """
    writer = FakeWriter(
        replies=["Complexity rose 40%.", "`added.py:1` — parameter_count 6 against a ceiling of 5."]
    )
    comment = write_review(PAYLOAD, writer)

    assert not comment.refused, comment
    assert comment.attempts == 2
    assert "40" in writer.prompts[1], "the retry has to say which number was rejected"
    assert "Complexity rose 40%." in writer.prompts[1], "and show the reply it came from"


def test_the_writer_is_never_shown_the_diff() -> None:
    """A model given code reviews the code. This one restates measurements.

    It is also what keeps a workflow triggered from a fork from handing that fork's contents
    to a language model.
    """
    writer = FakeWriter(replies=["No findings."])
    write_review({**PAYLOAD, "findings": [], "counts": {"new": 0}}, writer)

    assert "def " not in writer.prompts[0], writer.prompts[0]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(16.0, "16"), (16, "16"), ("1,939", "1939"), (0.14, "0.14"), (True, "")],
)
def test_a_number_is_compared_in_the_spelling_a_sentence_would_use(value, expected) -> None:
    """`16.0` and `16` are one number and two strings, and a model writes the second."""
    assert numeral(value) == expected


def test_a_ratio_may_be_quoted_as_a_percentage() -> None:
    """A measurement holding `0.9875` is quoted as `98.75`, and refusing that would refuse
    the natural sentence."""
    allowed = quotable_numbers({"call_coverage": 0.9875})

    assert unquotable("coverage is 98.75%", allowed) == []
    assert unquotable("coverage is 0.9875", allowed) == []
    assert unquotable("coverage is 99", allowed) == ["99"]


def test_the_quotable_set_includes_numbers_inside_messages() -> None:
    """A finding's `message` already spells the comparison a comment will repeat."""
    allowed = quotable_numbers(PAYLOAD)

    assert unquotable("parameter_count 6, above the ceiling of 5", allowed) == []


def test_an_empty_reply_is_a_refusal_not_an_empty_comment() -> None:
    comment = write_review(PAYLOAD, FakeWriter(replies=["", ""]))

    assert comment == ReviewComment(invented=(), model="fake", attempts=2)
    assert comment.refused


# ---- the comment OXN writes itself ---------------------------------------------------------


def test_the_body_is_written_without_a_model_at_all() -> None:
    """A GitHub runner has no Ollama host, and a review with nothing to post is not a review.

    This is also the floor the refusal falls to, which is what makes refusing affordable: the
    rule is that unaudited prose may not carry an unmeasured number, not that a pull request
    goes unanswered because a model misbehaved.
    """
    from oxn.writers import review_body

    body = review_body({**PAYLOAD, "base": "main", "head": "HEAD", "measured": STAMP})

    assert "1 new" in body
    assert "`added.py:1`" in body
    assert unquotable(body, quotable_numbers({**PAYLOAD, "measured": STAMP})) == [], body


def test_the_writer_that_cannot_lie_is_held_to_the_same_rule() -> None:
    """Asserted rather than asserted-by-inspection: `review_body` composes no number, so the
    honesty check must pass over its output for every payload shape, including the empty one.
    """
    from oxn.writers import review_body

    empty = {
        "status": "OK",
        "base": "main",
        "head": "HEAD",
        "measured": STAMP,
        "files": {"changed": 0, "measured": 0, "excluded": 0},
        "counts": {"new": 0, "regression": 0, "baselined": 0},
        "findings": [],
        "errors": [],
    }
    for payload in (empty, {**PAYLOAD, "base": "main", "head": "HEAD", "measured": STAMP}):
        body = review_body(payload)
        assert unquotable(body, quotable_numbers(payload)) == [], body


def test_a_refused_prose_still_posts_the_measurement(repo: Path, monkeypatch) -> None:
    """The narrowing of what refusing means, at the surface that acts on it.

    `status` still says REFUSED and `invented` still names the numbers -- the model gets no
    credit for sentences it did not earn -- but `body` carries OXN's own summary, because the
    measurement was already made and withholding it punishes the author for the writer's fault.
    """
    import oxn.writers as writers
    from oxn.report import run_review_report

    monkeypatch.setattr(
        writers, "ollama_writer", lambda model="": FakeWriter(replies=["Up 40%.", "Still 40%."])
    )
    payload = run_review_report("main", "HEAD", write="ollama")

    assert payload["comment"]["status"] == "REFUSED"
    assert payload["comment"]["invented"] == ["40"]
    assert "**OXN**" in payload["comment"]["body"], "the review survives the writer"


def test_without_a_writer_the_comment_is_oxn_own(repo: Path) -> None:
    from oxn.report import run_review_report

    comment = run_review_report("main", "HEAD")["comment"]

    assert comment == {
        "status": "OK",
        "body": comment["body"],
        "model": "oxn",
        "invented": [],
        "attempts": 0,
    }
    assert "parameter_count" in comment["body"]


def test_an_unknown_writer_is_named_and_the_review_still_happens(repo: Path) -> None:
    from oxn.report import run_review_report

    comment = run_review_report("main", "HEAD", write="anthropic")["comment"]

    assert comment["status"] == "REFUSED"
    assert "anthropic" in comment["note"]
    assert "**OXN**" in comment["body"]


def test_the_writer_is_not_handed_the_bag_of_quotable_numbers() -> None:
    """`quotable` is the audit trail for a refusal, not an input to the prose.

    A model shown a list of integers and told to use numbers from it writes numerically-valid
    nonsense: the numbers belong to the findings they were measured on. It stays in the JSON
    `oxn review` prints, where a person auditing a refusal wants exactly that bag.
    """
    writer = FakeWriter(replies=["No findings."])
    write_review({**PAYLOAD, "quotable": ["7777"]}, writer)

    assert "7777" not in writer.prompts[0], writer.prompts[0]
    assert "quotable" not in writer.prompts[0]


# ---- the trigger ---------------------------------------------------------------------------


def test_the_workflow_is_opt_in_and_never_overwrites_an_edited_one(tmp_path) -> None:
    """A repository that has not asked for a bot in its pull requests should not get one.

    And an existing file is left alone rather than merged: a workflow is a policy document
    about who may run what with which token, and merging someone's edits to that is not a
    favour.
    """
    from oxn.init import run_init

    plain = run_init(tmp_path, with_hook=False, with_mcp=False)
    workflow = tmp_path / ".github" / "workflows" / "oxn-review.yml"
    assert not workflow.exists(), plain.as_dict()

    asked = run_init(tmp_path, with_hook=False, with_mcp=False, with_github=True)
    assert workflow.exists()
    assert ".github/workflows/oxn-review.yml" in asked.created

    workflow.write_text("# edited by hand\n")
    again = run_init(tmp_path, with_hook=False, with_mcp=False, with_github=True)
    assert workflow.read_text() == "# edited by hand\n"
    assert ".github/workflows/oxn-review.yml" in again.unchanged


def test_the_workflow_parses_and_runs_oxn_review_without_gating(tmp_path) -> None:
    """The trigger cannot be tested end to end here; that it is well-formed can be.

    The assertions are the properties that make it safe rather than merely valid:
    `issue_comment` runs the workflow from the *base* repository, so a fork cannot rewrite the
    job that reviews it; the token may write comments and not code; and nothing in the job
    fails the build, because the `oxn check` job is the gate.
    """
    yaml = pytest.importorskip("yaml")
    from oxn.init import _WORKFLOW

    parsed = yaml.safe_load(_WORKFLOW)
    job = parsed["jobs"]["review"]

    assert "issue_comment" in parsed[True], "`on:` parses as True in YAML 1.1"
    assert parsed["permissions"] == {"contents": "read", "pull-requests": "write"}
    assert "github.event.issue.pull_request" in job["if"], "an issue has no diff to measure"
    assert "@oxn" in job["if"]
    assert "fetch-depth: 0" in _WORKFLOW, "the merge base is not in a shallow clone"
    assert "oxn check" not in _runs(job), "the review never gates; CI's check job does"


def test_the_workflow_posts_the_body_oxn_rendered_rather_than_building_its_own(tmp_path) -> None:
    """One owner for the comment, and it is the one the suite can reach.

    The first version of this job rebuilt the summary in inline Python from `review.json`. That
    is a second renderer living in YAML where no test runs -- and it meant `--write` could
    never take effect there, so a file documenting a model-written comment produced one OXN had
    written. `oxn review` renders the body now and the job posts it.
    """
    yaml = pytest.importorskip("yaml")
    from oxn.init import _WORKFLOW

    runs = _runs(yaml.safe_load(_WORKFLOW)["jobs"]["review"])

    assert "oxn review" in runs
    assert ".comment.body" in runs, "the job posts what OXN rendered"
    assert "import json" not in runs, "and does not rebuild it"
    assert "OXN_OLLAMA_HOST" in _WORKFLOW, (
        "a runner has no model host, so the file has to say what it would take to get prose"
    )


def _runs(job: dict) -> str:
    """Every shell command in a job, as one string to search."""
    return " ".join(str(step.get("run", "")) for step in job["steps"])
