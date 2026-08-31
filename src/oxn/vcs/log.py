"""Parse ``git log`` into commits and file changes.

All the correctness risk of the VCS tier lives here, so the parser handles the cases that
actually occur rather than the happy path:

* **Renames arrive in two syntaxes.** ``--numstat`` emits either ``old => new`` or the
  brace form ``dir/{a => b}/file.py``. Both are normalized, and a rename chain maps every
  historical path onto the file's *current* path -- otherwise a renamed file's history
  silently splits in two and its churn halves.
* **Binary files** emit ``-`` instead of line counts.
* **Merge commits are excluded** by default; including them double-counts every change.
* **Author identity goes through ``.mailmap``** (``--use-mailbox``/``--use-mailmap``), or
  ownership metrics are noise.
* **Field separators are NUL**, not a printable character: any printable delimiter can and
  does appear in commit messages.
* **Non-ASCII paths** need ``core.quotepath=false``.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable, Sequence

#: Record and field markers. A commit message can contain any printable character -- and
#: real ones do contain ``=>``, ``|`` and tabs -- so the separators must be non-printable.
#: They cannot be written literally into the format argument either: a NUL byte terminates
#: a string in ``argv``, so ``subprocess`` rejects it. git's own ``%xNN`` placeholders let
#: git emit the bytes instead.
_RECORD = "\x1e"
_FIELD = "\x00"
_RECORD_FORMAT = "%x1e"
_FIELD_FORMAT = "%x00"

_BRACE_RENAME = re.compile(r"^(.*)\{(.*) => (.*)\}(.*)$")


class GitLogError(RuntimeError):
    """Raised when git cannot be run or the path is not a repository."""


@dataclass(frozen=True, slots=True)
class FileChange:
    """One file's change within one commit."""

    path: str
    added: int
    deleted: int
    old_path: str | None = None

    @property
    def is_binary(self) -> bool:
        return self.added == 0 and self.deleted == 0 and self.old_path is None

    @property
    def churn(self) -> int:
        return self.added + self.deleted


@dataclass(frozen=True, slots=True)
class Commit:
    """One commit, with the files it touched."""

    sha: str
    author: str
    email: str
    when: datetime
    subject: str
    changes: tuple[FileChange, ...] = ()

    @property
    def size(self) -> int:
        return len(self.changes)


@dataclass
class History:
    """A repository's history, with paths resolved to their current names."""

    commits: list[Commit] = field(default_factory=list)
    #: Historical path -> current path, following renames forward.
    renames: dict[str, str] = field(default_factory=dict)

    @property
    def span(self) -> tuple[datetime, datetime] | None:
        if not self.commits:
            return None
        moments = [commit.when for commit in self.commits]
        return min(moments), max(moments)


def read_log(
    repo: Path | str = ".",
    *,
    paths: Sequence[str] | None = None,
    include_merges: bool = False,
    max_commits: int | None = None,
    since: str | None = None,
) -> History:
    """Read history from ``repo``.

    Paths in the result are repo-relative POSIX and resolved through renames to the name
    the file carries *today*, which is what makes them join against the code graph.
    """
    command = [
        "git",
        "-c",
        "core.quotepath=false",
        "log",
        "--use-mailmap",
        "--numstat",
        "--date=iso-strict",
        "-M",
        "-C",
        "--find-renames=50%",
        "--pretty=format:" + _RECORD_FORMAT + _FIELD_FORMAT.join(["%H", "%aN", "%aE", "%ad", "%s"]),
    ]
    if not include_merges:
        command.append("--no-merges")
    if max_commits:
        command.append(f"-n{max_commits}")
    if since:
        command.append(f"--since={since}")
    if paths:
        command.extend(["--", *paths])

    try:
        result = subprocess.run(command, cwd=str(repo), capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:  # pragma: no cover - git absent
        raise GitLogError("git is not installed or not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise GitLogError(f"git log failed in {repo}: {exc.stderr.strip()}") from exc

    return parse_log(result.stdout)


def parse_log(output: str) -> History:
    """Parse the output of the command :func:`read_log` builds.

    Two passes, because a rename chain is only knowable once the whole log has been read:
    a file renamed A -> B -> C appears as two separate renames in two commits, and every
    change to A must end up attributed to C.
    """
    history = History()
    raw_commits = [
        commit for block in output.split(_RECORD) if (commit := _parse_commit(block, history))
    ]

    resolved = _resolve_chains(history.renames)
    history.renames = resolved
    history.commits = [_rename_paths(commit, resolved) for commit in raw_commits]
    return history


def _parse_commit(block: str, history: History) -> Commit | None:
    """One record: the header fields, then its numstat lines.

    Renames are recorded into `history` as they are seen, because resolving a chain needs
    all of them and the caller does that afterwards.
    """
    if not block.strip():
        return None
    header, _, body = block.partition("\n")
    fields = header.split(_FIELD)
    if len(fields) < 5:
        return None
    sha, author, email, when, subject = fields[:5]

    changes: list[FileChange] = []
    for line in body.splitlines():
        change = _parse_numstat(line)
        if change is None:
            continue
        changes.append(change)
        if change.old_path:
            history.renames[change.old_path] = change.path

    return Commit(
        sha=sha,
        author=author,
        email=email,
        when=_parse_date(when),
        subject=subject,
        changes=tuple(changes),
    )


def _rename_paths(commit: Commit, resolved: dict[str, str]) -> Commit:
    """Re-point every change at the name its file carries *today*.

    That is what makes a history join against the code graph: a hotspot for `b.py` is not
    findable if half its churn is recorded against the name it had last year.
    """
    return Commit(
        sha=commit.sha,
        author=commit.author,
        email=commit.email,
        when=commit.when,
        subject=commit.subject,
        changes=tuple(
            FileChange(
                path=resolved.get(change.path, change.path),
                added=change.added,
                deleted=change.deleted,
                old_path=change.old_path,
            )
            for change in commit.changes
        ),
    )


def _parse_numstat(line: str) -> FileChange | None:
    parts = line.split("\t")
    if len(parts) != 3:
        return None
    added_raw, deleted_raw, path_raw = parts
    # Binary files report `-` rather than counts.
    added = 0 if added_raw == "-" else int(added_raw)
    deleted = 0 if deleted_raw == "-" else int(deleted_raw)
    old_path, path = _split_rename(path_raw.strip())
    return FileChange(path=path, added=added, deleted=deleted, old_path=old_path)


def _split_rename(raw: str) -> tuple[str | None, str]:
    """Normalize both rename spellings into ``(old, new)``."""
    match = _BRACE_RENAME.match(raw)
    if match:
        prefix, old, new, suffix = match.groups()
        old_path = f"{prefix}{old}{suffix}".replace("//", "/")
        new_path = f"{prefix}{new}{suffix}".replace("//", "/")
        return old_path, new_path
    if " => " in raw:
        old_path, _, new_path = raw.partition(" => ")
        return old_path.strip(), new_path.strip()
    return None, raw


def _resolve_chains(renames: dict[str, str]) -> dict[str, str]:
    """Follow ``a -> b -> c`` so every historical path maps to the current one."""
    resolved: dict[str, str] = {}
    for start in renames:
        seen = {start}
        current = start
        while current in renames and renames[current] not in seen:
            current = renames[current]
            seen.add(current)
        resolved[start] = current
    return resolved


def _parse_date(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(raw)
    except ValueError:  # pragma: no cover - unusual date formats
        return datetime.fromtimestamp(0).astimezone()


def source_changes(commits: Iterable[Commit], *, exclude: Iterable[str] = ()) -> list[Commit]:
    """Drop paths OXN does not analyse, and commits left empty as a result.

    Without this a lockfile or a vendored directory dominates every churn ranking.
    """
    from oxn.profiles import profile_for_path

    patterns = tuple(exclude)
    kept: list[Commit] = []
    for commit in commits:
        changes = tuple(
            change
            for change in commit.changes
            if profile_for_path(change.path) is not None
            and not any(pattern in change.path for pattern in patterns)
        )
        if changes:
            kept.append(
                Commit(
                    sha=commit.sha,
                    author=commit.author,
                    email=commit.email,
                    when=commit.when,
                    subject=commit.subject,
                    changes=changes,
                )
            )
    return kept
