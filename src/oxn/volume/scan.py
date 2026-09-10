"""Repo-scoped analysis: duplication, erosion, and history.

These are *report-path* operations. Clone detection compares every file against every
other, and history reads the whole log, so neither belongs anywhere near the hook's
per-edit budget (ADR-0002).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from oxn.thresholds import DUPLICATION_MIN_LINES, DUPLICATION_MIN_TOKENS
from oxn.volume.clones import DuplicationReport, file_windows, find_clones
from oxn.volume.erosion import ErosionReport, structural_erosion

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable, Sequence

    from oxn.graph.indexer import Indexer
    from oxn.vcs.analysis import ChangeCoupling, Hotspot


@dataclass
class VolumeReport:
    """Everything Tier 1.5 and Tier 2.5 say about a codebase."""

    duplication: DuplicationReport
    erosion: ErosionReport
    hotspots: list[Hotspot] = field(default_factory=list)
    file_metrics: dict[str, dict[str, float]] = field(default_factory=dict)
    #: Pairs that change together more often than coincidence explains. D'Ambros, Lanza &
    #: Robbes (WCRE 2009) measured Spearman above 0.5 against defects on three systems and
    #: above 0.8 on Eclipse -- "more than metrics but less than number of changes".
    coupling: list[ChangeCoupling] = field(default_factory=list)
    #: Smallest set of authors owning more than half the files. A blunt instrument, reported
    #: as one: it counts files, not knowledge.
    bus_factor: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "duplication": {
                "clone_classes": len(self.duplication.clone_classes),
                "duplicate_tokens": self.duplication.duplicate_tokens,
                "total_tokens": self.duplication.total_tokens,
                "verbosity": round(self.duplication.duplication_ratio, 4),
            },
            "erosion": self.erosion.as_dict(),
            "hotspots": [spot.as_dict() for spot in self.hotspots],
            "coupling": [
                {
                    "first": pair.first,
                    "second": pair.second,
                    "support": pair.support,
                    "confidence": round(pair.confidence, 3),
                }
                for pair in self.coupling[:25]
            ],
            "bus_factor": self.bus_factor,
        }


def scan_duplication(
    indexer: Indexer,
    paths: Sequence[Path],
    *,
    min_tokens: int = DUPLICATION_MIN_TOKENS,
    min_lines: int = DUPLICATION_MIN_LINES,
) -> DuplicationReport:
    """Find clones across the analysed set.

    Reparses rather than reading the cache: window hashes are not persisted yet, and the
    honest thing is to pay the cost visibly rather than pretend the cache covers it.
    """
    from oxn.graph.sources import iter_source_files
    from oxn.languages import get_parser
    from oxn.profiles import profile_for_path

    windows = {}
    for path in iter_source_files(paths):
        profile = profile_for_path(str(path))
        if profile is None:
            continue
        source = path.read_bytes()
        tree = get_parser(profile.name).parse(source)
        if tree.root_node.has_error:
            continue
        relative = indexer.relative(path)
        windows[relative] = file_windows(relative, tree.root_node, profile, min_tokens=min_tokens)

    return find_clones(windows, min_tokens=min_tokens, min_lines=min_lines)


def scan_volume(
    indexer: Indexer,
    paths: Sequence[Path],
    *,
    repo: Path | None = None,
    include_history: bool = True,
) -> VolumeReport:
    """Duplication, erosion and -- when the tree is a git repository -- hotspots."""
    duplication = scan_duplication(indexer, paths)
    erosion = structural_erosion(indexer.store.entity_scores())

    report = VolumeReport(duplication=duplication, erosion=erosion)
    duplicate_lines = duplication.duplicate_lines_by_file()
    for path, lines in duplicate_lines.items():
        report.file_metrics.setdefault(path, {})["duplicate_lines"] = float(len(lines))

    if include_history:
        report.hotspots = _history_metrics(indexer, repo or indexer.root, report)

    for values in report.file_metrics.values():
        values.setdefault("duplicate_lines", 0.0)
    return report


def _history_metrics(indexer: Indexer, repo: Path, report: VolumeReport) -> list[Hotspot]:
    """Churn, ownership and co-change, when history is available.

    Everything here reads the *same* parsed commit stream the hotspot rank already needed, so
    the three additions cost one more pass rather than another `git log`. Measured on a
    synthetic 50,000-commit, 10,000-file history: co-change 2.1 s, ownership 0.5 s. `oxn
    volume` is a report command and not the hook path, which is what makes that affordable.
    """
    from oxn.vcs.analysis import bus_factor, change_coupling, file_histories, hotspots, ownership
    from oxn.vcs.log import GitLogError, read_log, source_changes

    per_file = report.file_metrics
    try:
        history = read_log(repo)
    except GitLogError:
        return []

    commits = source_changes(history.commits)
    histories = file_histories(commits)
    for path, record in histories.items():
        values = per_file.setdefault(path, {})
        values["commits"] = float(record.commits)
        values["churn"] = float(record.churn)
        values["authors"] = float(record.author_count)

    # Bird et al. (FSE 2011) on Vista and Windows 7: the count of low-expertise contributors
    # correlated with pre-release failures at 0.86 and 0.93, above size, churn and every
    # complexity metric Microsoft collected. It is the strongest single number in the
    # literature this project surveyed, and OXN computed it and showed it to nobody.
    owners = ownership(histories)
    for path, owner in owners.items():
        values = per_file.setdefault(path, {})
        values["minor_contributors"] = float(owner.minor_contributors)
        values["top_share"] = round(owner.top_share, 3)
    report.bus_factor = bus_factor(owners)
    report.coupling = change_coupling(commits)

    spots = hotspots(histories, indexer.store.complexity_by_file(), limit=25)
    for spot in spots:
        per_file.setdefault(spot.path, {})["hotspot"] = spot.score
    return spots


def persist(indexer: Indexer, report: VolumeReport) -> None:
    """Write file-scoped measurements into the cache so the CLI can rank by them."""
    if report.file_metrics:
        indexer.store.put_file_metrics(report.file_metrics)


def iter_paths(raw: Iterable[str]) -> list[Path]:
    return [Path(item) for item in raw]
