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

**It does not re-measure the base.** ``origin`` on a finding says whether `.oxn/baseline.json`
already knew about it, which is the ratchet the whole project runs on -- so ``new`` means *not
in the baseline*, and not *caused by this pull request*. A new violation can sit in an
unchanged function of a changed file. Telling the two apart needs a second checkout and a
second measurement; until that exists the word means what this paragraph says it means.
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
    command = ["git", "diff", "--name-only", f"{base}...{head}"]
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command, cwd=str(repo), capture_output=True, text=True, check=True
        )
    except FileNotFoundError as exc:  # pragma: no cover - git absent
        raise GitLogError("git is not installed or not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise GitLogError(
            f"git diff {base}...{head} failed in {repo}: {exc.stderr.strip()}"
        ) from exc

    root = Path(repo)
    return [line for line in result.stdout.splitlines() if line and (root / line).is_file()]


def numeral(value: float | int | str) -> str:
    """One number in the spelling a comment would use, so both sides compare as text.

    `16.0` and `16` are the same number and different strings, and a model writes the second.
    Thousands separators are stripped for the same reason: the measurement says `1939` and
    the prose says `1,939`.
    """
    if isinstance(value, str):
        return value.replace(",", "")
    if isinstance(value, bool):  # `bool` is an `int`, and `True` is not a number here
        return ""
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
    payload: dict[str, Any] = {
        "status": "FINDINGS" if _actionable(report) else "OK",
        "base": base,
        "head": head,
        "measured": _stamp(repo, settings),
        "files": {
            "changed": len(touched),
            "measured": len(measurable),
            "excluded": len(touched) - len(measurable),
        },
        "findings": _findings(report),
        "counts": {
            "new": len(report.findings),
            "regression": len(report.regressed),
            "baselined": len(report.baselined),
        },
        "errors": report.errors,
    }
    payload["quotable"] = sorted(quotable_numbers({k: v for k, v in payload.items()}))
    return payload


def _actionable(report: CheckReport) -> bool:
    """Is there anything here the author has to do? Baselined debt is not."""
    return bool(report.findings or report.regressed)


def _findings(report: CheckReport) -> list[dict[str, Any]]:
    """Every finding, each carrying where it stands with the baseline.

    **Per finding, not as three counts.** A pull request that touches one line of a long
    function otherwise gets that function's length reported as though the author wrote it, and
    a review that opens with a violation the author did not cause is the false positive that
    ends adoption. `origin` is what lets the writer lead with what is new and mention the rest
    as pre-existing.
    """
    tagged: list[dict[str, Any]] = []
    for origin, findings in (
        ("new", report.findings),
        ("regression", report.regressed),
        ("baselined", report.baselined),
    ):
        tagged.extend({**finding.as_dict(), "origin": origin} for finding in findings)
    return tagged


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

    return {
        "commit": _head_sha(repo),
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
