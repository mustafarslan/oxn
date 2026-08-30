"""Behavioural metrics derived from history.

Citations, because a threshold without provenance is folklore:

* **Churn** -- Nagappan & Ball, "Use of relative code churn measures to predict system
  defect density", *ICSE 2005*. The *relative* measures were the predictive ones, so those
  are reported alongside the absolutes.
* **Change coupling** -- Gall, Hajek & Jazayeri, *ICSM 1998*; Zimmermann, Weissgerber,
  Diehl & Zeller, *TSE* 31(6), 2005.
* **Hotspots** -- Tornhill, *Your Code as a Crime Scene* (2015). Complexity times change
  frequency. OXN uses cognitive complexity as the complexity term, which is a better proxy
  than the usual line count and is already computed.
* **Ownership** -- Bird, Nagappan, Murphy, Gall & Devanbu, "Don't touch my code!",
  *FSE 2011*. The count of *minor* contributors was the strongest defect correlate.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from typing import TYPE_CHECKING

from oxn.thresholds import (
    CHANGE_COUPLING_MAX_COMMIT_SIZE,
    CHANGE_COUPLING_MIN_CONFIDENCE,
    CHANGE_COUPLING_MIN_SUPPORT,
    MINOR_CONTRIBUTOR_SHARE,
)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable, Mapping, Sequence

    from oxn.vcs.log import Commit


@dataclass
class FileHistory:
    """What history says about one file."""

    path: str
    commits: int = 0
    added: int = 0
    deleted: int = 0
    authors: Counter[str] = field(default_factory=Counter)
    first_seen: datetime | None = None
    last_seen: datetime | None = None

    @property
    def churn(self) -> int:
        return self.added + self.deleted

    @property
    def age_days(self) -> float | None:
        """Days since the file was last touched. High age plus high churn means volatility
        that has since settled; low age plus high churn means it is still moving."""
        if self.last_seen is None:
            return None
        now = datetime.now(timezone.utc)
        return (now - self.last_seen.astimezone(timezone.utc)).total_seconds() / 86400

    @property
    def author_count(self) -> int:
        return len(self.authors)


def file_histories(commits: Iterable[Commit]) -> dict[str, FileHistory]:
    """Aggregate per-file history. Paths are already resolved through renames."""
    histories: dict[str, FileHistory] = {}
    for commit in commits:
        for change in commit.changes:
            record = histories.setdefault(change.path, FileHistory(path=change.path))
            record.commits += 1
            record.added += change.added
            record.deleted += change.deleted
            record.authors[commit.author] += 1
            if record.first_seen is None or commit.when < record.first_seen:
                record.first_seen = commit.when
            if record.last_seen is None or commit.when > record.last_seen:
                record.last_seen = commit.when
    return histories


@dataclass(frozen=True, slots=True)
class ChangeCoupling:
    """Two files that keep changing together."""

    first: str
    second: str
    support: int
    first_commits: int
    second_commits: int

    @property
    def confidence(self) -> float:
        """P(second changes | first changes). Asymmetric, so the max of both directions."""
        forward = self.support / self.first_commits if self.first_commits else 0.0
        backward = self.support / self.second_commits if self.second_commits else 0.0
        return max(forward, backward)

    @property
    def jaccard(self) -> float:
        union = self.first_commits + self.second_commits - self.support
        return self.support / union if union else 0.0


def change_coupling(
    commits: Iterable[Commit],
    *,
    min_support: int = CHANGE_COUPLING_MIN_SUPPORT,
    min_confidence: float = CHANGE_COUPLING_MIN_CONFIDENCE,
    max_commit_size: int = CHANGE_COUPLING_MAX_COMMIT_SIZE,
) -> list[ChangeCoupling]:
    """Files that change together more often than coincidence explains.

    ``max_commit_size`` is the filter that makes this work at all: one reformatting commit
    or squashed merge touching 400 files would otherwise couple everything to everything.
    """
    per_file: Counter[str] = Counter()
    pairs: Counter[tuple[str, str]] = Counter()

    for commit in commits:
        touched = sorted({change.path for change in commit.changes})
        if len(touched) > max_commit_size:
            continue
        per_file.update(touched)
        for pair in combinations(touched, 2):
            pairs[pair] += 1

    results = [
        ChangeCoupling(first, second, support, per_file[first], per_file[second])
        for (first, second), support in pairs.items()
        if support >= min_support
    ]
    results = [coupling for coupling in results if coupling.confidence >= min_confidence]
    results.sort(key=lambda coupling: (-coupling.support, -coupling.confidence))
    return results


def sum_of_coupling(couplings: Sequence[ChangeCoupling]) -> dict[str, int]:
    """Total co-change weight per file -- the "change magnets"."""
    totals: dict[str, int] = defaultdict(int)
    for coupling in couplings:
        totals[coupling.first] += coupling.support
        totals[coupling.second] += coupling.support
    return dict(totals)


@dataclass(frozen=True, slots=True)
class Hotspot:
    """A file that is both complex and frequently changed."""

    path: str
    commits: int
    churn: int
    complexity: float
    score: float

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "path": self.path,
            "commits": self.commits,
            "churn": self.churn,
            "complexity": self.complexity,
            "score": round(self.score, 4),
        }


def hotspots(
    histories: Mapping[str, FileHistory],
    complexity: Mapping[str, float],
    *,
    limit: int = 25,
) -> list[Hotspot]:
    """Rank files by change frequency times complexity.

    Both axes are normalized to ``[0, 1]`` before multiplying, so neither a very old
    repository nor a very large file can dominate by unit scale alone. Both raw values are
    kept on the result, because "why is this a hotspot" needs them.
    """
    shared = [path for path in histories if path in complexity]
    if not shared:
        return []

    max_commits = max(histories[path].commits for path in shared) or 1
    max_complexity = max(complexity[path] for path in shared) or 1.0

    ranked = [
        Hotspot(
            path=path,
            commits=histories[path].commits,
            churn=histories[path].churn,
            complexity=complexity[path],
            score=(histories[path].commits / max_commits) * (complexity[path] / max_complexity),
        )
        for path in shared
    ]
    ranked.sort(key=lambda spot: -spot.score)
    return ranked[:limit]


@dataclass(frozen=True, slots=True)
class Ownership:
    """Who knows this file."""

    path: str
    top_author: str
    top_share: float
    author_count: int
    minor_contributors: int

    @property
    def is_orphaned(self) -> bool:
        """No author holds a majority -- nobody clearly owns it."""
        return self.top_share < 0.5


def ownership(
    histories: Mapping[str, FileHistory], *, minor_share: float = MINOR_CONTRIBUTOR_SHARE
) -> dict[str, Ownership]:
    """Ownership share and minor-contributor count per file."""
    results: dict[str, Ownership] = {}
    for path, history in histories.items():
        if not history.authors:
            continue
        total = sum(history.authors.values())
        top_author, top_commits = history.authors.most_common(1)[0]
        minor = sum(1 for count in history.authors.values() if count / total < minor_share)
        results[path] = Ownership(
            path=path,
            top_author=top_author,
            top_share=top_commits / total,
            author_count=len(history.authors),
            minor_contributors=minor,
        )
    return results


def bus_factor(owners: Mapping[str, Ownership]) -> int:
    """Smallest number of authors who together own more than half the files.

    A blunt instrument, and reported as one: it counts files, not knowledge.
    """
    if not owners:
        return 0
    tally = Counter(record.top_author for record in owners.values())
    half = len(owners) / 2
    running = 0
    for index, (_, count) in enumerate(tally.most_common(), start=1):
        running += count
        if running > half:
            return index
    return len(tally)
