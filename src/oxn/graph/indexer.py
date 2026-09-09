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
from oxn.graph.model import Entity, EntityKind, EntityMetrics, MetricValue
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

        aggregate_classes(self.store, seen)
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


#: Entities that can own methods; mirrors `metrics.coupling`, which does the join.
_OWNER_KINDS = frozenset({EntityKind.CLASS, EntityKind.INTERFACE})


def aggregate_classes(store: GraphStore, paths: set[str]) -> None:
    """Recompute every class aggregate at *package* scope, once all files are indexed.

    `metrics.engine` answers per file, which is exact wherever a method is nested in its
    type's body -- every language OXN supports except Go. Go declares a method at file scope
    carrying a receiver, so a type's methods can sit in a sibling file and one tree cannot
    see them; left per-file, `Counter` reads NOM 0 and both class ceilings are inert for the
    language.

    They are recomputed for *every* language rather than only Go, so that one definition
    reaches the gate. A ceiling is compared against `.oxn/baseline.json`, and an aggregate
    whose value depends on which file the gate happened to be measuring cannot be compared
    against anything -- "worse" would be a comparison between two different numbers.

    Module-level rather than a method on `Indexer`: the class it would join is already at
    its own `weighted_methods_per_class` ceiling, and this is one of the rules that says so.
    """
    from oxn.metrics.coupling import package_class_totals

    for members in _by_package(paths).values():
        entities = [entity for path in members for entity in store.entities_for(path)]
        measurements = {path: store.measurements_for(path) for path in members}
        if not _has_work(entities, measurements):
            continue
        totals, fragments = package_class_totals(entities, _cyclomatic(measurements))
        _rewrite(store, members, measurements, totals, fragments)


def _has_work(
    entities: list[Entity], measurements: dict[str, dict[str, dict[str, MetricValue]]]
) -> bool:
    """Whether this package has both a type to own methods and measurements to attribute.

    The second half is not paranoia: an indexer built with `measure=False` stores no metrics,
    and writing totals derived from nothing would replace real values with zeros.
    """
    return any(entity.kind in _OWNER_KINDS for entity in entities) and any(measurements.values())


def _cyclomatic(
    measurements: dict[str, dict[str, dict[str, MetricValue]]],
) -> dict[str, float]:
    """Entity id -> cyclomatic complexity, across a whole package. WMC's weight."""
    return {
        entity_id: values["cyclomatic_complexity"].value
        for found in measurements.values()
        for entity_id, values in found.items()
        if "cyclomatic_complexity" in values
    }


def _by_package(paths: set[str]) -> dict[str, list[str]]:
    """Group indexed files by the directory that holds them, which is Go's package."""
    grouped: dict[str, list[str]] = {}
    for path in paths:
        grouped.setdefault(path.rpartition("/")[0], []).append(path)
    return grouped


def _rewrite(
    store: GraphStore,
    members: list[str],
    measurements: dict[str, dict[str, dict[str, MetricValue]]],
    totals: dict[str, tuple[int, float]],
    fragments: dict[str, str],
) -> None:
    """Write the package-scope totals back through the store's ordinary per-file write.

    Rebuilding each file's `EntityMetrics` rather than patching two rows is what keeps this
    off `GraphStore`'s public surface: `put_metrics` already replaces a file's measurements,
    and adding a second write path for two keys would have grown the very class this metric
    exists to bound -- it is baselined at 27 methods, and the attempt failed the gate.

    `put_metrics` reads only `entity_id` and `values`, so the remaining fields are
    placeholders rather than a claim about what each entity is.
    """
    for path in members:
        found = measurements[path]
        if not (totals.keys() | fragments.keys()) & found.keys():
            continue
        store.put_metrics(
            path,
            [
                EntityMetrics(
                    entity_id,
                    "",
                    EntityKind.CLASS,
                    0,
                    _with_totals(values, totals.get(entity_id), entity_id in fragments),
                )
                for entity_id, values in found.items()
            ],
        )


#: The aggregates this pass owns. Written on the type, and taken off anything that turned
#: out to be a fragment of one.
_CLASS_AGGREGATES = ("nom", "wmc")


def _with_totals(
    values: dict[str, MetricValue], carried: tuple[int, float] | None, is_fragment: bool
) -> dict[str, MetricValue]:
    """One entity's measurements, with the package-scope aggregates substituted in.

    A fragment loses them outright. `metrics.engine` answers per file and per block, so a
    Rust `impl` carries a count of the methods written in *it*; once those are counted on the
    type, leaving the fragment's own would publish two answers to one question and let the
    smaller be the one a ceiling is compared against.
    """
    if is_fragment:
        return {key: value for key, value in values.items() if key not in _CLASS_AGGREGATES}
    if carried is None:
        return dict(values)
    return {
        **values,
        "nom": MetricValue("nom", carried[0]),
        "wmc": MetricValue("wmc", carried[1]),
    }
