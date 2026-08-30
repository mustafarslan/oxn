"""Walk a tree, parse what OXN understands, and keep the cache current.

This is the layer the hook and the report path share. The only difference between them is
scope: one file versus a repository.

Incrementality is the whole point. A no-op re-run must be dominated by hashing, not
parsing, or the hook's latency budget is spent re-deriving facts that have not changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from oxn.graph.builder import build_file, content_sha
from oxn.graph.sources import iter_source_files
from oxn.graph.store import DEFAULT_CACHE_PATH, GraphStore
from oxn.profiles import profile_for_path

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable

    from oxn.graph.model import ParsedFile


@dataclass
class IndexReport:
    """What one indexing run did. ``cached`` versus ``parsed`` is the incrementality signal."""

    parsed: int = 0
    cached: int = 0
    skipped: int = 0
    failed: int = 0
    incomplete: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def considered(self) -> int:
        return self.parsed + self.cached

    @property
    def cache_hit_ratio(self) -> float:
        return self.cached / self.considered if self.considered else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "parsed": self.parsed,
            "cached": self.cached,
            "skipped": self.skipped,
            "failed": self.failed,
            "cache_hit_ratio": round(self.cache_hit_ratio, 4),
            "parse_incomplete": self.incomplete,
            "errors": self.errors,
        }


class Indexer:
    """Keeps a :class:`~oxn.graph.store.GraphStore` in step with the working tree."""

    def __init__(
        self,
        root: Path | str = ".",
        cache_path: Path | str = DEFAULT_CACHE_PATH,
        *,
        measure: bool = True,
    ) -> None:
        self.root = Path(root).resolve()
        self.store = GraphStore(cache_path)
        self.measure = measure
        self._parsers: dict[str, object] = {}

    def _parser(self, language: str) -> object:
        if language not in self._parsers:
            from oxn.languages import get_parser

            self._parsers[language] = get_parser(language)
        return self._parsers[language]

    def grammar_version(self) -> str:
        """Identifies the grammar set. Part of the cache key, so a pack upgrade invalidates."""
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("tree-sitter-language-pack")
        except PackageNotFoundError:  # pragma: no cover
            return "unknown"

    def relative(self, path: Path) -> str:
        """Cache keys are repo-relative and POSIX, so a cache survives being moved."""
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return path.resolve().as_posix()

    def index_file(self, path: Path, *, force: bool = False) -> tuple[ParsedFile | None, bool]:
        """Index one file. Returns ``(parsed, was_cached)``; ``parsed`` is ``None`` when cached."""
        profile = profile_for_path(str(path))
        if profile is None:
            raise ValueError(f"no LanguageProfile for {path}")

        source = path.read_bytes()
        rel = self.relative(path)
        sha = content_sha(source)
        grammar_version = self.grammar_version()

        if not force and self.store.is_current(rel, sha, profile.version, grammar_version):
            return None, True

        parser = self._parser(profile.name)
        tree = parser.parse(source)  # type: ignore[attr-defined]
        parsed = build_file(rel, source, profile, tree.root_node)
        self.store.put_file(parsed, profile.version, grammar_version)

        # Measuring in the same pass reuses the tree that is already in hand; parsing twice
        # would double the cost of the hook path for no benefit.
        if self.measure:
            from oxn.metrics.engine import measure_file

            measured = measure_file(list(parsed.entities), source, profile, tree.root_node)
            self.store.put_metrics(rel, measured)

        return parsed, False

    def index(self, roots: Iterable[Path] | None = None, *, force: bool = False) -> IndexReport:
        """Index a tree, pruning cache entries for files that no longer exist."""
        targets = list(roots) if roots is not None else [self.root]
        report = IndexReport()
        seen: set[str] = set()

        for path in iter_source_files(targets):
            rel = self.relative(path)
            seen.add(rel)
            try:
                parsed, was_cached = self.index_file(path, force=force)
            except OSError as exc:
                report.failed += 1
                report.errors[rel] = f"{type(exc).__name__}: {exc}"
                continue
            if was_cached:
                report.cached += 1
            else:
                report.parsed += 1
                if parsed is not None and parsed.parse_incomplete:
                    report.incomplete.append(rel)

        if roots is None:
            for stale in self.store.known_paths() - seen:
                self.store.forget(stale)

        return report

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> Indexer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
