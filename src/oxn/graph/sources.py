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

    The second kind now prunes the walk as well as filtering it, but **only where pruning
    cannot lose a file**. `fnmatch`'s ``*`` crosses ``/``, so for a pattern ending in ``*`` a
    directory match guarantees every path beneath it matches too; for ``vendored/*b`` it does
    not -- ``vendored/xb`` matches and ``vendored/xb/c.py`` does not. So only trailing-``*``
    patterns are handed to the walk, and everything else is filtered exactly as before.
    """
    project = (base or Path.cwd()).resolve()
    pruning = tuple(pattern for pattern in exclude if pattern.endswith("*"))
    for root in roots:
        found = _one_file(root) if root.is_file() else _below(root, excludes, project, pruning)
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

    `resolve()` is a `realpath` syscall per file and this runs on every candidate the walk
    produces, so it is skipped for the paths that provably do not need it -- exactly as
    `Indexer.relative` does, and with the same `..` guard, since `relative_to` does not
    normalize and a glob must be matched against one spelling of a path rather than two.
    Only projects that declare `exclude` ever paid this, which is why it hid: OXN's own
    configuration declares one and the corpus it was benchmarked against does not.
    """
    return bool(patterns) and any(
        fnmatch(_as_written(path, project), pattern) for pattern in patterns
    )


def _as_written(path: Path, project: Path) -> str:
    """The path the way `oxn.yaml` writes it: relative to the project root, POSIX, no `./`.

    A path outside that root cannot be described by a repo-relative glob, so it is matched
    absolute and simply will not hit one.

    `resolve()` is a `realpath` syscall and this runs on every candidate the walk produces, so
    it is skipped for the paths that provably do not need it -- exactly as `Indexer.relative`
    does, and with the same `..` guard, since `relative_to` does not normalize and a glob must
    be matched against one spelling of a path rather than two.
    """
    if ".." not in path.parts:
        try:
            return path.relative_to(project).as_posix()
        except ValueError:
            pass
    resolved = path.resolve()
    try:
        return resolved.relative_to(project).as_posix()
    except ValueError:
        return resolved.as_posix()


def _below(
    root: Path, excludes: frozenset[str], project: Path, pruning: Sequence[str] = ()
) -> Iterator[Path]:
    """Every analysable file under a directory, skipping what is not ours to measure.

    **A subtree the project excludes entirely is not entered.** `exclude` filters files, and
    for a long time it filtered them *after* the walk had found them -- so OXN's own hook
    descended into `benchmarks/corpora`, 5,165 directories and 18,259 files, ran
    `profile_for_path` on every one and discarded them all by glob. Measured 2026-09-17:
    131 ms per walk against 1.3 ms once the subtree is pruned, 4,585 directories visited
    against 33, and `_edge_facts` walks the tree on every hook run.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        # In place, because `os.walk` reads this list back to decide where to descend.
        dirnames[:] = [
            name
            for name in dirnames
            if _worth_entering(name, excludes)
            and not _out_of_scope(Path(dirpath) / name, project, pruning)
        ]
        for filename in filenames:
            candidate = Path(dirpath) / filename
            if profile_for_path(str(candidate)) is not None:
                yield candidate


def _worth_entering(name: str, excludes: frozenset[str]) -> bool:
    return name not in excludes and not name.startswith(".")
