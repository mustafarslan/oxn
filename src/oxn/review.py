"""`oxn review`: measure what a pull request changed, and hand the numbers to a writer.

The split this module exists to enforce: **OXN produces the numbers and a language model
produces the sentences, and the second may never invent the first.** That is ADR-0002's line
between what may block and what may only inform, applied to a comment on a pull request rather
than to a metric — and here it is mechanical. `unquotable` extracts every numeral from the
prose and refuses anything the measurement does not contain.

Two things this deliberately does not do.

**It does not read the diff.** A review that quotes code is reviewing code, and this is not
that: it restates measurements about files a diff touched. Keeping the hunks out of the prompt
is the same honesty rule one level up, and it also means a fork's contents are never handed to
a model by a workflow triggered from that fork.

**``origin`` and ``introduced`` answer two different questions, and both are reported.**
``origin`` says whether `.oxn/baseline.json` already knew about the finding -- the ratchet the
whole project runs on. ``introduced`` says whether the pull request *caused* it, which needs
the base measured too: a violation that is ``new`` to the baseline can sit in an untouched
function of a changed file, and telling a reviewer the author wrote it is the false positive
that ends adoption. `_at_base` checks out the merge base into a throwaway worktree and measures
the same paths **under the head's `oxn.yaml`**, so a ceiling that tightened does not read as
code that worsened. Where that cannot be done -- no git, a shallow clone, a base that does not
resolve -- ``introduced`` is ``null`` rather than guessed, and `oxn review` says so.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from oxn.vcs.log import GitLogError

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable, Sequence

    from oxn.check import CheckReport
    from oxn.config import Config

#: Any numeral a comment might contain: `16`, `1,939`, `0.14`, `98.75`.
_NUMERAL = re.compile(r"\d[\d,]*(?:\.\d+)?")


def changed_files(repo: Path | str, base: str, head: str) -> list[str]:
    """Repo-relative paths the range touched, as ``git diff --name-only`` reports them.

    Three dots, not two: `base...head` is the diff against the *merge base*, which is what a
    pull request shows. Two dots would report every change made on the base branch since the
    fork point as though the author had made it.

    Deleted paths are dropped -- there is nothing left to measure -- and that is done by
    asking the filesystem rather than by parsing a status column, because a rename reports the
    new name and the old one is simply gone either way.
    """
    out = _git(repo, "diff", "--name-only", f"{base}...{head}")
    root = Path(repo)
    return [line for line in out.splitlines() if line and (root / line).is_file()]


def changed_lines(repo: Path | str, base: str, head: str) -> dict[str, set[int]]:
    """Line numbers on the **head** side that the range touched, per file.

    **A review comment can only be placed on a line the diff contains.** GitHub rejects one
    anywhere else with a 422, and that rule is the whole of "position mapping": a finding on an
    untouched line of a touched file -- which is most of them, since a ceiling is about a whole
    function -- has no line to hang a comment on and belongs in the summary instead.

    `--unified=0` so a hunk header names exactly the added lines and no surrounding context;
    context lines are not part of the diff for this purpose and a comment on one is refused.
    Deletions produce `+start,0` and contribute nothing, which is correct: a line that is gone
    cannot carry a comment.
    """
    out = _git(repo, "diff", "--unified=0", f"{base}...{head}")
    touched: dict[str, set[int]] = {}
    path = ""
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@") and path:
            touched.setdefault(path, set()).update(_hunk_lines(line))
    return touched


def _hunk_lines(header: str) -> range:
    """The head-side lines one `@@ -a,b +c,d @@` header covers."""
    after = header.split("+", 1)[-1].split("@@", 1)[0].strip()
    start, _, count = after.partition(",")
    try:
        first, length = int(start), int(count) if count else 1
    except ValueError:  # pragma: no cover - malformed header
        return range(0)
    return range(first, first + length)


def numeral(value: float | int | str) -> str:
    """One number in the spelling a comment would use, so both sides compare as text.

    `16.0` and `16` are the same number and different strings, and a model writes the second.
    Thousands separators are stripped for the same reason: the measurement says `1939` and
    the prose says `1,939`.
    """
    if isinstance(value, bool):  # `bool` is an `int`, and `True` is not a number here
        return ""
    if isinstance(value, str):
        # **Normalised the same way a float is, and that is the whole point of one spelling.**
        # A measurement holding `value: 16.0` puts `16` in the quotable set, because that is
        # what a sentence says. A model that instead copies the JSON verbatim writes `16.0` --
        # the *measured* number, quoted exactly -- and until 2026-09-14 that was refused as
        # invented, because the string branch only stripped commas. Measured on `kimi-k3:cloud`
        # against a payload whose findings carry `value`/`ceiling` and no `message`: it did
        # this on 3 of 3 first attempts, was told `16.0` was not in the measurement, and
        # complied by writing `16`. The honesty rule was firing on honesty and the retry was
        # teaching a model to stop quoting its source.
        stripped = value.replace(",", "")
        try:
            return numeral(float(stripped))
        except ValueError:
            return stripped  # `1.7.0`, `2026-09-14`: not a number, compared as written
    if float(value).is_integer():
        return str(int(value))
    return f"{float(value):g}"


def quotable_numbers(payload: Any) -> set[str]:
    """Every number a comment about ``payload`` is allowed to state.

    Walks the whole structure rather than listing fields, because the alternative is a list
    that goes stale the first time the payload gains a key -- and a number the writer may use
    but this set forgot is a refusal the author cannot act on.

    A percentage is admitted in both spellings: a measurement holding `0.9875` is quoted as
    either `0.9875` or `98.75`, and refusing the second would refuse the natural sentence.
    """
    found: set[str] = set()
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
        else:
            found |= _spellings(current)
    return {value for value in found if value}


def _spellings(value: Any) -> set[str]:
    """Every way one leaf could legitimately appear in a sentence.

    A percentage is admitted in both: a measurement holding `0.9875` is quoted as either
    `0.9875` or `98.75`, and refusing the second would refuse the natural sentence. A string
    contributes the numerals inside it, because a finding's `message` already says
    "cognitive_complexity 16, above the ceiling of 12" and those are the numbers a comment
    will repeat.
    """
    if isinstance(value, bool):
        return set()
    if isinstance(value, (int, float)):
        spelled = {numeral(value)}
        if 0.0 < float(value) < 1.0:
            spelled.add(numeral(round(float(value) * 100, 4)))
        return spelled
    if isinstance(value, str):
        return {numeral(match.group()) for match in _NUMERAL.finditer(value)}
    return set()


def unquotable(prose: str, allowed: Iterable[str]) -> list[str]:
    """Numbers the prose states that the measurement does not contain, in order of appearance.

    **Strict, and the strictness is the point.** "Python 3.11" and "top 5" are refused unless
    the measurement happens to hold those numbers, and the prompt tells the writer not to
    introduce numerals for exactly that reason. The alternative -- matching a number only
    against the metric named beside it -- is a parser that has to understand English, and a
    review tool that is subtly wrong about its own honesty rule is worse than one that
    occasionally asks for a sentence to be rewritten.
    """
    permitted = set(allowed)
    seen: list[str] = []
    for match in _NUMERAL.finditer(prose):
        value = numeral(match.group())
        if value not in permitted and value not in seen:
            seen.append(value)
    return seen


def run_review(
    base: str = "origin/main",
    head: str = "HEAD",
    *,
    repo: Path | str = ".",
    config: Config | None = None,
) -> dict[str, Any]:
    """Measure the files a pull request touched. Returns the payload a writer may quote from.

    **File-scoped, with no way to ask for more.** The architectural tier is repository-scoped,
    so a deep review reports findings in files the pull request never opened -- which is the
    false positive that ends adoption, dressed as thoroughness. A `--deep` flag was written
    here and then removed: offering it would have contradicted this paragraph, and an option
    nobody should choose is a decision left to the person least able to make it. The CI gate
    job is where a repository-wide answer belongs.
    """
    from oxn.check import run_check
    from oxn.config import Config as _Config

    settings = config or _Config.load(Path(repo))
    touched = changed_files(repo, base, head)
    measurable = _measurable(touched, settings)

    report = run_check([str(Path(repo) / path) for path in measurable], config=settings)
    tagged = _findings(report, _at_base(repo, base, measurable, settings))
    payload: dict[str, Any] = {
        "status": "FINDINGS" if _actionable(report) else "OK",
        "base": base,
        "head": head,
        "measured": _stamp(repo, settings),
        "files": {
            "changed": len(touched),
            "measured": len(measurable),
            "excluded": len(touched) - len(measurable),
            # Counted here rather than in the comment that reports it. `errors` is a mapping of
            # path to reason, and `quotable_numbers` walks a dict's values -- so a body saying
            # `len(errors)` would be stating a number the measurement does not contain, and
            # passing the check only when that count happened to appear somewhere else.
            "errored": len(report.errors),
        },
        "findings": tagged,
        "comments": _line_comments(tagged, changed_lines(repo, base, head)),
        "counts": _counts(report, tagged),
        "errors": report.errors,
    }
    payload["quotable"] = sorted(quotable_numbers({k: v for k, v in payload.items()}))
    return payload


def _actionable(report: CheckReport) -> bool:
    """Is there anything here the author has to do? Baselined debt is not."""
    return bool(report.findings or report.regressed)


def _at_base(
    repo: Path | str, base: str, paths: Sequence[str], settings: Config
) -> dict[str, float] | None:
    """Every finding the base commit already had, keyed the way `Finding.key` keys them.

    **A throwaway worktree, not a stash or a checkout.** `git worktree add --detach` leaves the
    working tree the caller is standing in completely alone, which matters because this runs
    inside CI jobs and on developer machines with uncommitted work. It is removed in a `finally`
    even when the measurement raises.

    **The head's `oxn.yaml` is applied to the base's code**, by replacing only the root on the
    settings already loaded. Measuring the base under its own configuration would report a
    ceiling that tightened as code that got worse -- the author would be shown a finding they
    did not cause, which is the whole thing this function exists to prevent.

    Returns ``None`` rather than an empty mapping when the base cannot be measured: "the base
    had no findings" and "nobody looked" are different claims, and only one of them licenses
    telling an author they introduced something.
    """
    import dataclasses
    import shutil
    import tempfile

    from oxn.check import run_check

    worktree = Path(tempfile.mkdtemp(prefix="oxn-base-"))
    try:
        _git(repo, "worktree", "add", "--detach", str(worktree), base)
    except GitLogError:
        shutil.rmtree(worktree, ignore_errors=True)
        return None
    try:
        existing = [str(worktree / path) for path in paths if (worktree / path).is_file()]
        report = run_check(existing, config=dataclasses.replace(settings, root=worktree))
        return {
            _rebase_key(finding.key, worktree): finding.value
            for finding in [*report.findings, *report.regressed, *report.baselined]
        }
    finally:
        _git(repo, "worktree", "remove", "--force", str(worktree), check=False)
        shutil.rmtree(worktree, ignore_errors=True)


def _rebase_key(key: str, worktree: Path) -> str:
    """A base finding's key, with the worktree prefix taken off its path.

    `Finding.key` is `rule|path|entity` and the path is relative to the root it was measured
    under, so a key from the worktree and one from the checkout never match until the two roots
    are made to agree.
    """
    return key.replace(f"{worktree}/", "").replace(str(worktree), "")


def _git(repo: Path | str, *args: str, check: bool = True) -> str:
    """One git command in `repo`, raising `GitLogError` with git's own words."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],  # noqa: S607
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=check,
        )
    except FileNotFoundError as exc:  # pragma: no cover - git absent
        raise GitLogError("git is not installed or not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise GitLogError(f"git {' '.join(args)} failed: {exc.stderr.strip()}") from exc
    return result.stdout


def _findings(report: CheckReport, at_base: dict[str, float] | None) -> list[dict[str, Any]]:
    """Every finding, each carrying where it stands with the baseline.

    **Per finding, not as three counts.** A pull request that touches one line of a long
    function otherwise gets that function's length reported as though the author wrote it, and
    a review that opens with a violation the author did not cause is the false positive that
    ends adoption. `origin` is what lets the writer lead with what is new and mention the rest
    as pre-existing.

    **`introduced` is the second question and the one an author actually asks.** `origin: new`
    means the baseline had not seen it; `introduced: true` means the base commit did not have
    it either, so this pull request caused it. They come apart exactly where it matters -- a
    long function that was already over the ceiling and was never baselined is `new` and *not*
    introduced, and reporting it as the author's is how a review stops being read. `was` carries
    the base's value where there is one, so "19, was 16" is a sentence made of measured numbers.

    Both are `null` when the base could not be measured, which is not the same as `false`.
    """
    tagged: list[dict[str, Any]] = []
    for origin, findings in (
        ("new", report.findings),
        ("regression", report.regressed),
        ("baselined", report.baselined),
    ):
        for finding in findings:
            row: dict[str, Any] = {**finding.as_dict(), "origin": origin}
            row["introduced"] = None if at_base is None else finding.key not in at_base
            row["was"] = None if at_base is None else at_base.get(finding.key)
            tagged.append(row)
    return tagged


def _line_comments(
    tagged: Sequence[dict[str, Any]], touched: dict[str, set[int]]
) -> list[dict[str, Any]]:
    """Findings that can be said *on the line*, in the shape GitHub's review API takes.

    **Two filters, and both are about not being wrong in public.** A comment may only sit on a
    line the diff contains, or the API refuses the whole review with a 422 -- so a ceiling
    finding about a function whose signature line the author never touched has no anchor and
    stays in the summary. And only findings this pull request `introduced` are placed: a line
    comment is the most attributable thing a bot can write, and putting one on pre-existing debt
    the author merely stood next to is the false positive that gets a reviewer muted.

    `introduced` is `null` when the base could not be measured, and `null` is not `true`: no
    base, no line comments, and the summary says everything it would have said anyway.
    """
    footer = "\n\n<sub>OXN. This does not gate.</sub>"
    comments: list[dict[str, Any]] = []
    for row in tagged:
        if row.get("introduced") is not True or row["origin"] == "baselined":
            continue
        if row["line"] not in touched.get(row["path"], ()):
            continue
        comments.append(
            {
                "path": row["path"],
                "line": row["line"],
                "side": "RIGHT",
                "body": f"**{row['rule']}** — {row['message']}{footer}",
            }
        )
    return comments


def _counts(report: CheckReport, tagged: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The three baseline counts, and the one that says what this pull request caused.

    `introduced` is `null` rather than 0 where the base could not be measured. "This pull
    request introduced nothing" and "nobody checked" are different claims, and only the first
    belongs in a comment.
    """
    actionable = [row for row in tagged if row["origin"] != "baselined"]
    known = [row["introduced"] for row in actionable if row["introduced"] is not None]
    return {
        "new": len(report.findings),
        "regression": len(report.regressed),
        "baselined": len(report.baselined),
        "introduced": sum(known) if len(known) == len(actionable) else None,
    }


def _measurable(paths: Sequence[str], settings: Config) -> list[str]:
    """The changed files OXN has a profile for and `oxn.yaml` does not exclude.

    The exclusion has to be applied here as well as in the walk: these paths come from `git`,
    which has never read `oxn.yaml`, and a review that measures vendored or generated code is
    the same defect `oxn index` had -- two commands disagreeing about what is ours.
    """
    from fnmatch import fnmatch

    from oxn.profiles import profile_for_path

    return [
        path
        for path in paths
        if profile_for_path(path) is not None
        and not any(fnmatch(path, pattern) for pattern in settings.exclude)
    ]


def _stamp(repo: Path | str, settings: Config) -> dict[str, Any]:
    """What was measured, so a comment can be recognised as stale.

    A pull request gets pushed to, and a comment quoting numbers from three commits ago reads
    exactly like a comment quoting numbers from this one. ADR-0004 makes the same argument for
    the MCP server's stamp: a stale answer that cannot be identified as stale is worse than no
    answer.
    """
    from oxn import __version__
    from oxn.profiles import PROFILES

    commit = _head_sha(repo)
    return {
        "commit": commit,
        # The abbreviation a comment shows, measured rather than sliced off at the point of
        # use: `commit[:7]` of a sha beginning with eight digits is a numeral the payload does
        # not contain, and a footer is the last place anyone would look for that.
        "short": commit[:7],
        "oxn_version": __version__,
        "profiles": {name: profile.version for name, profile in sorted(PROFILES.items())},
        "ceilings": dict(sorted(settings.ceilings.items())),
    }


def _head_sha(repo: Path | str) -> str:
    """The commit measured, or ``""`` where there is no git to ask."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return ""
    return result.stdout.strip()
