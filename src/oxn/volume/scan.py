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
    from oxn.vcs.analysis import Hotspot


@dataclass
class VolumeReport:
    """Everything Tier 1.5 and Tier 2.5 say about a codebase."""

    duplication: DuplicationReport
    erosion: ErosionReport
    hotspots: list[Hotspot] = field(default_factory=list)
    file_metrics: dict[str, dict[str, float]] = field(default_factory=dict)

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
    from oxn.graph.indexer import iter_source_files
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

    per_file: dict[str, dict[str, float]] = {}
    duplicate_lines = duplication.duplicate_lines_by_file()
    for path, lines in duplicate_lines.items():
        per_file.setdefault(path, {})["duplicate_lines"] = float(len(lines))

    spots: list[Hotspot] = []
    if include_history:
        spots = _history_metrics(indexer, repo or indexer.root, per_file)

    for values in per_file.values():
        values.setdefault("duplicate_lines", 0.0)

    return VolumeReport(
        duplication=duplication,
        erosion=erosion,
        hotspots=spots,
        file_metrics=per_file,
    )


def _history_metrics(
    indexer: Indexer, repo: Path, per_file: dict[str, dict[str, float]]
) -> list[Hotspot]:
    """Churn, commit counts and hotspot rank, when history is available."""
    from oxn.vcs.analysis import file_histories, hotspots
    from oxn.vcs.log import GitLogError, read_log, source_changes

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
