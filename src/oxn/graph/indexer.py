"""Walk a tree, parse what OXN understands, and keep the cache current.

This is the layer the hook and the report path share. The only difference between them is
scope: one file versus a repository.

Incrementality is the whole point. A no-op re-run must be dominated by hashing, not
parsing, or the hook's latency budget is spent re-deriving facts that have not changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from oxn.graph.builder import build_file, content_sha
from oxn.graph.sources import iter_source_files
from oxn.graph.store import DEFAULT_CACHE_PATH, GraphStore
from oxn.profiles import profile_for_path

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable, Sequence

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
        exclude: Sequence[str] = (),
    ) -> None:
        self.root = Path(root).resolve()
        self.store = GraphStore(cache_path)
        self.measure = measure
        #: `oxn.yaml`'s `exclude` globs. Held here so that every caller asking this indexer
        #: for files gets the same answer, and so the walk skips them before parsing rather
        #: than after: excluded code that is parsed and then dropped costs the same as
        #: excluded code that is measured.
        self.exclude = tuple(exclude)
        self._parsers: dict[str, object] = {}

    def sources(self, roots: Iterable[Path] | None = None) -> list[Path]:
        """Every file this indexer will look at under `roots`, exclusions applied.

        The single answer to "which files", so a caller cannot measure one set and report
        another. `Config.exclude` was accepted and validated by `oxn.yaml` for four phases
        while no code read it; asking the indexer is how that stops being possible.
        """
        return list(
            iter_source_files(
                list(roots) if roots is not None else [self.root],
                base=self.root,
                exclude=self.exclude,
            )
        )

    def _parser(self, language: str) -> object:
        if language not in self._parsers:
            from oxn.languages import get_parser

            self._parsers[language] = get_parser(language)
        return self._parsers[language]

    def grammar_version(self) -> str:
        """Identifies the grammar set. Part of the cache key, so a pack upgrade invalidates."""
        return _grammar_version()

    def relative(self, path: Path) -> str:
        """Cache keys are repo-relative and POSIX, so a cache survives being moved.

        `resolve()` is a `realpath` syscall, and this is called once per file: 43.6 ms of
        the whole-tree path on nest's 1,913 files, twice what the directory walk itself
        costs. It is only *needed* for a path that is relative, or that reaches the tree by
        a route `relative_to` cannot subtract -- so try the pure-text answer first and pay
        the syscall on the paths that need it. `os.walk` does not follow symlinks and
        `self.root` is already resolved, so every path the walk yields takes the fast branch
        and gets the same string it did before.

        **`..` disqualifies the fast branch**, because `relative_to` does not normalize:
        `/repo/src/../src/x.py` comes back as `src/../src/x.py` where `resolve()` gives
        `src/x.py`. That is a different string, and this string is a cache key, a
        `Finding.path` and therefore a third of a baseline key -- so the same file named two
        ways would be two violations, and accepting one would not accept the other. The walk
        never produces such a path; a person on a command line and a foreign hook payload
        both can.
        """
        if ".." not in path.parts:
            try:
                return path.relative_to(self.root).as_posix()
            except ValueError:
                pass
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

    def _record(self, path: Path, rel: str, report: IndexReport, *, force: bool) -> None:
        """Index one file into `report`, turning an unreadable file into a counted error.

        `OSError` only: a file that cannot be read is a fact about the tree, not a bug in
        OXN, and one unreadable file must not abandon the other nine thousand. Anything
        else propagates, because a parser or profile failure is ours and should be loud.
        """
        try:
            parsed, was_cached = self.index_file(path, force=force)
        except OSError as exc:
            report.failed += 1
            report.errors[rel] = f"{type(exc).__name__}: {exc}"
            return
        if was_cached:
            report.cached += 1
            return
        report.parsed += 1
        if parsed is not None and parsed.parse_incomplete:
            report.incomplete.append(rel)

    def index(self, roots: Iterable[Path] | None = None, *, force: bool = False) -> IndexReport:
        """Index a tree, pruning cache entries for files that no longer exist."""
        targets = list(roots) if roots is not None else [self.root]
        report = IndexReport()
        seen: set[str] = set()

        for path in self.sources(targets):
            rel = self.relative(path)
            seen.add(rel)
            self._record(path, rel, report, force=force)

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


@lru_cache(maxsize=1)
def _grammar_version() -> str:
    """The installed grammar pack's version, read once.

    Part of every cache key, so it is asked for once per file -- and
    `importlib.metadata.version` walks the installed distributions each time it is called.
    On a 1,913-file tree that was 1,913 lookups and 0.42 s, entirely to re-read a string
    that cannot change while the process is alive.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("tree-sitter-language-pack")
    except PackageNotFoundError:  # pragma: no cover
        return "unknown"
