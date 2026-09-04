"""Which files OXN will look at, and which it refuses to.

Separate from the indexer because *finding source files* is not orchestration. Every tier
needs this walk -- the metric engine, the clone scanner, the resolution grader -- and when it
lived beside `Indexer`, importing it dragged the analysis layer into a dependency on the
orchestration layer. OXN's own layer contract caught that, three times over, which is a
better argument for the split than any amount of taste.

The exclusion set is the load-bearing part: generated and vendored code otherwise dominates
every metric, which `docs/metrics.md` calls the difference between a signal and noise.
"""

from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

from oxn.profiles import profile_for_path

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable, Iterator, Sequence


#: Never analysed. Generated and vendored code otherwise dominates every metric --
#: docs/metrics.md makes this the difference between a signal and noise.
DEFAULT_EXCLUDES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".oxn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        "target",
        "vendor",
        "third_party",
        ".tox",
        ".next",
        ".nuxt",
        "site-packages",
    }
)


def iter_source_files(
    roots: Iterable[Path],
    excludes: frozenset[str] = DEFAULT_EXCLUDES,
    *,
    base: Path | None = None,
    exclude: Sequence[str] = (),
) -> Iterator[Path]:
    """Yield every file OXN has a profile for, beneath ``roots``.

    A root may be a file or a directory, and the two cases have nothing in common beyond the
    question they answer, so each has its own function.

    Two kinds of exclusion, and they are not the same claim:

    * ``excludes`` names *directories OXN never walks into* -- ``.git``, ``node_modules``,
      ``dist``. A root that is a file passes through them, because naming a file is asking
      for it by hand.
    * ``exclude`` is the project's own ``oxn.yaml`` list: path globs, matched against the
      path relative to ``base``, saying *this code is not ours to measure*. That claim holds
      however the file is reached, named or walked to -- otherwise the hook, which now always
      names the one file the agent edited, would gate the generated code a project just
      declared out of scope.
    """
    project = (base or Path.cwd()).resolve()
    for root in roots:
        found = _one_file(root) if root.is_file() else _below(root, excludes)
        yield from (path for path in found if not _out_of_scope(path, project, exclude))


def _one_file(path: Path) -> Iterator[Path]:
    """A root that is a file is taken as named, directory exclusions and all: it was asked for."""
    if profile_for_path(str(path)) is not None:
        yield path


def _out_of_scope(path: Path, project: Path, patterns: Sequence[str]) -> bool:
    """Does the project's own `exclude` list name this file?

    Matched the way `oxn.yaml` writes paths: relative to the project root, POSIX, no `./`.
    A path outside that root cannot be described by a repo-relative glob, so it is matched
    absolute and simply will not hit one.
    """
    if not patterns:
        return False
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(project).as_posix()
    except ValueError:
        relative = resolved.as_posix()
    return any(fnmatch(relative, pattern) for pattern in patterns)


def _below(root: Path, excludes: frozenset[str]) -> Iterator[Path]:
    """Every analysable file under a directory, skipping what is not ours to measure."""
    for dirpath, dirnames, filenames in os.walk(root):
        # In place, because `os.walk` reads this list back to decide where to descend.
        dirnames[:] = [name for name in dirnames if _worth_entering(name, excludes)]
        for filename in filenames:
            candidate = Path(dirpath) / filename
            if profile_for_path(str(candidate)) is not None:
                yield candidate


def _worth_entering(name: str, excludes: frozenset[str]) -> bool:
    return name not in excludes and not name.startswith(".")
