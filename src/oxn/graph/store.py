"""SQLite persistence for the code graph.

**This database is a cache, not a system of record.** Everything in it is derivable from
the working tree, so a schema or profile change drops and rebuilds rather than migrates.
That single decision removes migrations from the project permanently.

Design constraints come straight from ADR-0002: the hook is a cold process with a ~200 ms
budget, so opening the store and reading one file's entities must be a handful of indexed
lookups, and the module must import nothing beyond the standard library.

Cache key -- a stored file is reusable only if **all** of these match:
``(path, content_sha, profile_version, grammar_version, schema_version)``. Forgetting
``profile_version`` is the classic failure: a profile fix ships, and every user silently
keeps results computed under the old node-kind tables.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from oxn.graph.model import (
    Edge,
    EdgeKind,
    Entity,
    EntityKind,
    EntityMetrics,
    MetricValue,
    ParsedFile,
    Provenance,
    Resolution,
)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Collection, Iterable, Iterator

#: Bump on any change to the statements below. A mismatch rebuilds the cache.
SCHEMA_VERSION = 5

DEFAULT_CACHE_PATH = Path(".oxn/cache/graph.db")

#: SQLite binds a bounded number of parameters per statement (999 before 3.32). Beyond this
#: many paths a query filters in Python instead -- still ranking within the requested set,
#: which is the property that matters.
_MAX_BOUND_PARAMS = 400

_SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE files (
    path             TEXT PRIMARY KEY,
    language         TEXT NOT NULL,
    content_sha      TEXT NOT NULL,
    size_bytes       INTEGER NOT NULL,
    profile_version  INTEGER NOT NULL,
    grammar_version  TEXT NOT NULL,
    parse_incomplete INTEGER NOT NULL,
    indexed_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE entities (
    id             TEXT PRIMARY KEY,
    file_path      TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    parent_id      TEXT,
    kind           TEXT NOT NULL,
    name           TEXT,
    qualified_name TEXT NOT NULL,
    start_byte     INTEGER NOT NULL,
    end_byte       INTEGER NOT NULL,
    start_line     INTEGER NOT NULL,
    end_line       INTEGER NOT NULL,
    attrs          TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX entities_by_file   ON entities(file_path);
CREATE INDEX entities_by_parent ON entities(parent_id);
CREATE INDEX entities_by_qname  ON entities(qualified_name);

-- Declared in full now so P4/P5 add rows rather than schema. Empty in P1.
CREATE TABLE edges (
    src_id     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    dst_id     TEXT,
    dst_ref    TEXT,
    provenance TEXT NOT NULL,
    resolution TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    attrs      TEXT NOT NULL DEFAULT '{}',
    file_path  TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE
);

-- Metric values carry their own exactness and provenance so the JSON handed to an agent
-- can never present an approximation as exact (ADR-0002).
CREATE TABLE metrics (
    entity_id   TEXT NOT NULL,
    metric_key  TEXT NOT NULL,
    value       REAL NOT NULL,
    exactness   TEXT NOT NULL DEFAULT 'EXACT',
    resolution  TEXT NOT NULL DEFAULT 'L0',
    -- Direction of error for an approximate value. Persisted because the gate policy of
    -- ADR-0002 reads it: an APPROX value may block a ceiling only when it is a *lower*
    -- bound. Dropping it on write meant a cached measurement could not be gated on at all.
    bound       TEXT NOT NULL DEFAULT 'exact',
    explanation TEXT,
    file_path   TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    PRIMARY KEY (entity_id, metric_key)
);

-- File-scoped measurements: duplication, verbosity, churn, hotspot rank. These belong to
-- a path rather than to a code entity, and history has no entity at all.
CREATE TABLE file_metrics (
    path       TEXT NOT NULL,
    metric_key TEXT NOT NULL,
    value      REAL NOT NULL,
    PRIMARY KEY (path, metric_key)
);

CREATE INDEX file_metrics_by_key ON file_metrics(metric_key, value);

-- SCIP symbol identities (ADR-0002, rung L2). Keeping them lets a later run resolve an
-- edge whose target lives in a file indexed earlier, and lets staleness be detected per
-- file rather than for the index as a whole.
CREATE TABLE symbols (
    symbol      TEXT PRIMARY KEY,
    entity_id   TEXT NOT NULL,
    file_path   TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    resolution  TEXT NOT NULL DEFAULT 'L2'
);

CREATE INDEX symbols_by_entity ON symbols(entity_id);
CREATE INDEX symbols_by_file   ON symbols(file_path);

CREATE INDEX metrics_by_file ON metrics(file_path);
CREATE INDEX metrics_by_key  ON metrics(metric_key, value);

CREATE INDEX edges_by_src  ON edges(src_id, kind);
CREATE INDEX edges_by_dst  ON edges(dst_id, kind);
CREATE INDEX edges_by_file ON edges(file_path);
"""


class GraphStore:
    """A code-graph cache backed by one SQLite file."""

    def __init__(self, path: Path | str = DEFAULT_CACHE_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._ensure_schema()

    def _configure(self) -> None:
        # WAL lets the long-lived MCP server read while a hook writes.
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")

    def _ensure_schema(self) -> None:
        found = (
            self._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if self._table_exists("meta")
            else None
        )

        if found is not None and int(found["value"]) == SCHEMA_VERSION:
            return

        # The store is a cache: on any mismatch, rebuild rather than migrate.
        for (name,) in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall():
            self._conn.execute(f"DROP TABLE IF EXISTS {name}")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
        )
        self._conn.commit()

    def _table_exists(self, name: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
            ).fetchone()
            is not None
        )

    # ---- cache protocol ---------------------------------------------------------

    def is_current(
        self, path: str, content_sha: str, profile_version: int, grammar_version: str
    ) -> bool:
        """True if this file's stored graph was built from exactly this input."""
        row = self._conn.execute(
            "SELECT content_sha, profile_version, grammar_version FROM files WHERE path = ?",
            (path,),
        ).fetchone()
        return row is not None and (
            row["content_sha"] == content_sha
            and row["profile_version"] == profile_version
            and row["grammar_version"] == grammar_version
        )

    def put_file(self, parsed: ParsedFile, profile_version: int, grammar_version: str) -> None:
        """Replace one file's contribution atomically."""
        with self._transaction() as conn:
            conn.execute("DELETE FROM files WHERE path = ?", (parsed.path,))
            conn.execute(
                "INSERT INTO files (path, language, content_sha, size_bytes, profile_version,"
                " grammar_version, parse_incomplete) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    parsed.path,
                    parsed.language,
                    parsed.content_sha,
                    parsed.size_bytes,
                    profile_version,
                    grammar_version,
                    int(parsed.parse_incomplete),
                ),
            )
            conn.executemany(
                "INSERT INTO entities (id, file_path, parent_id, kind, name, qualified_name,"
                " start_byte, end_byte, start_line, end_line, attrs)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        e.id,
                        e.file_path,
                        e.parent_id,
                        e.kind.value,
                        e.name,
                        e.qualified_name,
                        e.start_byte,
                        e.end_byte,
                        e.start_line,
                        e.end_line,
                        json.dumps(e.attrs, sort_keys=True),
                    )
                    for e in parsed.entities
                ],
            )
            conn.executemany(
                "INSERT INTO edges (src_id, kind, dst_id, dst_ref, provenance, resolution,"
                " confidence, attrs, file_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        edge.src_id,
                        edge.kind.value,
                        edge.dst_id,
                        edge.dst_ref,
                        edge.provenance.value,
                        edge.resolution.value,
                        edge.confidence,
                        json.dumps(edge.attrs, sort_keys=True),
                        parsed.path,
                    )
                    for edge in parsed.edges
                ],
            )

    def put_symbols(self, path: str, symbols: dict[str, str]) -> None:
        """Record ``SCIP symbol -> entity id`` for one file."""
        with self._transaction() as conn:
            conn.execute("DELETE FROM symbols WHERE file_path = ?", (path,))
            conn.executemany(
                "INSERT OR REPLACE INTO symbols (symbol, entity_id, file_path) VALUES (?, ?, ?)",
                [(symbol, entity_id, path) for symbol, entity_id in symbols.items()],
            )

    def symbol_table(self) -> dict[str, str]:
        """Every known ``symbol -> entity id``, for resolving cross-file edge targets."""
        return {
            row["symbol"]: row["entity_id"]
            for row in self._conn.execute("SELECT symbol, entity_id FROM symbols")
        }

    def put_edges(self, path: str, edges: list[Edge]) -> None:
        """Replace the non-containment edges belonging to one file."""
        with self._transaction() as conn:
            conn.execute("DELETE FROM edges WHERE file_path = ?", (path,))
            conn.executemany(
                "INSERT INTO edges (src_id, kind, dst_id, dst_ref, provenance, resolution,"
                " confidence, attrs, file_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        edge.src_id,
                        edge.kind.value,
                        edge.dst_id,
                        edge.dst_ref,
                        edge.provenance.value,
                        edge.resolution.value,
                        edge.confidence,
                        json.dumps(edge.attrs, sort_keys=True),
                        path,
                    )
                    for edge in edges
                ],
            )

    def resolve_edge_targets(self) -> int:
        """Fill in ``dst_id`` wherever an edge's ``dst_ref`` names a known symbol.

        Run after every file is indexed: an edge often points at a symbol defined in a file
        that had not been seen when the edge was created.
        """
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE edges SET dst_id ="
                " (SELECT entity_id FROM symbols s WHERE s.symbol = edges.dst_ref)"
                " WHERE dst_id IS NULL AND dst_ref IS NOT NULL"
                " AND EXISTS (SELECT 1 FROM symbols s WHERE s.symbol = edges.dst_ref)"
            )
            return int(cursor.rowcount)

    def edge_stats(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT kind, count(*) AS n, sum(dst_id IS NOT NULL) AS resolved"
            " FROM edges GROUP BY kind"
        ).fetchall()
        stats: dict[str, int] = {}
        for row in rows:
            stats[row["kind"]] = int(row["n"])
            stats[f"{row['kind']}_resolved"] = int(row["resolved"] or 0)
        return stats

    def put_metrics(self, path: str, measured: list[EntityMetrics]) -> None:
        """Replace this file's measurements."""
        rows = [
            (
                entity.entity_id,
                key,
                float(value.value),
                value.exactness,
                value.resolution.value,
                value.bound,
                json.dumps(list(value.explanation)) if value.explanation else None,
                path,
            )
            for entity in measured
            for key, value in entity.values.items()
        ]
        with self._transaction() as conn:
            conn.execute("DELETE FROM metrics WHERE file_path = ?", (path,))
            conn.executemany(
                "INSERT INTO metrics (entity_id, metric_key, value, exactness, resolution,"
                " bound, explanation, file_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def put_file_metrics(self, values: dict[str, dict[str, float]]) -> None:
        """Replace file-scoped measurements for the paths named."""
        with self._transaction() as conn:
            for path, metrics in values.items():
                conn.execute("DELETE FROM file_metrics WHERE path = ?", (path,))
                conn.executemany(
                    "INSERT INTO file_metrics (path, metric_key, value) VALUES (?, ?, ?)",
                    [(path, key, float(value)) for key, value in metrics.items()],
                )

    def file_metrics(
        self, metric_key: str, limit: int = 25, paths: Collection[str] | None = None
    ) -> list[tuple[str, float]]:
        """Highest values of one file-scoped metric. `paths` narrows before the limit."""
        sql = "SELECT path, value FROM file_metrics WHERE metric_key = ?"
        if paths is None:
            rows = self._conn.execute(
                f"{sql} ORDER BY value DESC LIMIT ?", (metric_key, limit)
            ).fetchall()
        elif len(paths) <= _MAX_BOUND_PARAMS:
            marks = ",".join("?" * len(paths))
            rows = self._conn.execute(
                f"{sql} AND path IN ({marks}) ORDER BY value DESC LIMIT ?",
                (metric_key, *paths, limit),
            ).fetchall()
        else:
            wanted = set(paths)
            ranked = self._conn.execute(f"{sql} ORDER BY value DESC", (metric_key,))
            rows = [row for row in ranked if row["path"] in wanted][:limit]
        return [(row["path"], row["value"]) for row in rows]

    def complexity_by_file(self, metric_key: str = "cognitive_complexity") -> dict[str, float]:
        """Total complexity per file, the term hotspot ranking multiplies by churn."""
        rows = self._conn.execute(
            "SELECT file_path, sum(value) AS total FROM metrics"
            " WHERE metric_key = ? GROUP BY file_path",
            (metric_key,),
        ).fetchall()
        return {row["file_path"]: float(row["total"]) for row in rows}

    def entity_scores(self, metric_key: str = "cognitive_complexity") -> dict[str, float]:
        """Qualified name -> value, for every measured entity. Feeds erosion."""
        rows = self._conn.execute(
            "SELECT e.qualified_name, m.value FROM metrics m"
            " JOIN entities e ON e.id = m.entity_id WHERE m.metric_key = ?",
            (metric_key,),
        ).fetchall()
        return {row["qualified_name"]: float(row["value"]) for row in rows}

    def metrics_for(self, path: str) -> dict[str, dict[str, float]]:
        """entity_id -> {metric key: value} for one file."""
        out: dict[str, dict[str, float]] = {}
        for row in self._conn.execute(
            "SELECT entity_id, metric_key, value FROM metrics WHERE file_path = ?", (path,)
        ):
            out.setdefault(row["entity_id"], {})[row["metric_key"]] = row["value"]
        return out

    def measurements_for(self, path: str) -> dict[str, dict[str, MetricValue]]:
        """entity_id -> {metric key: full measurement} for one file.

        `metrics_for` returns bare floats, which is enough to rank by but not enough to
        *gate* on: ADR-0002 lets an approximate value block a ceiling only when it is a
        lower bound, and a float has no bound.
        """
        out: dict[str, dict[str, MetricValue]] = {}
        for row in self._conn.execute(
            "SELECT entity_id, metric_key, value, exactness, resolution, bound, explanation"
            " FROM metrics WHERE file_path = ?",
            (path,),
        ):
            out.setdefault(row["entity_id"], {})[row["metric_key"]] = MetricValue(
                key=row["metric_key"],
                value=row["value"],
                exactness=row["exactness"],
                resolution=Resolution(row["resolution"]),
                bound=row["bound"],
                explanation=tuple(json.loads(row["explanation"]) if row["explanation"] else ()),
            )
        return out

    def worst(
        self, metric_key: str, limit: int = 20, paths: Collection[str] | None = None
    ) -> list[tuple[str, str, float]]:
        """Highest values of one metric, as (qualified name, path, value).

        `paths` restricts the ranking **before** the limit applies, and that ordering is the
        whole point. Ranking the graph first and filtering afterwards answers a different
        question -- "which of the project's worst functions happen to live here" -- and for
        a file whose functions are all healthier than the project's worst it answers with
        silence. A caller asking about one file must get that file's entities, however the
        rest of the repository looks.
        """
        sql = (
            "SELECT e.qualified_name, m.file_path, m.value FROM metrics m"
            " JOIN entities e ON e.id = m.entity_id"
            " WHERE m.metric_key = ?"
        )
        if paths is None:
            rows = self._conn.execute(
                f"{sql} ORDER BY m.value DESC LIMIT ?", (metric_key, limit)
            ).fetchall()
        elif len(paths) <= _MAX_BOUND_PARAMS:
            marks = ",".join("?" * len(paths))
            rows = self._conn.execute(
                f"{sql} AND m.file_path IN ({marks}) ORDER BY m.value DESC LIMIT ?",
                (metric_key, *paths, limit),
            ).fetchall()
        else:
            # More paths than SQLite will bind at once. Rank everything and filter here:
            # slower, but it still ranks within the requested set rather than outside it.
            wanted = set(paths)
            ranked = self._conn.execute(f"{sql} ORDER BY m.value DESC", (metric_key,))
            rows = [row for row in ranked if row["file_path"] in wanted][:limit]
        return [(r["qualified_name"], r["file_path"], r["value"]) for r in rows]

    def entities_for(self, path: str) -> list[Entity]:
        rows = self._conn.execute(
            "SELECT * FROM entities WHERE file_path = ? ORDER BY start_byte, end_byte DESC",
            (path,),
        ).fetchall()
        return [_entity_from_row(row) for row in rows]

    def edges_for(self, path: str) -> list[Edge]:
        rows = self._conn.execute("SELECT * FROM edges WHERE file_path = ?", (path,)).fetchall()
        return [_edge_from_row(row) for row in rows]

    def forget(self, path: str) -> None:
        """Drop a file that no longer exists in the working tree."""
        with self._transaction() as conn:
            conn.execute("DELETE FROM files WHERE path = ?", (path,))

    def known_paths(self) -> set[str]:
        return {row["path"] for row in self._conn.execute("SELECT path FROM files")}

    def stats(self) -> dict[str, Any]:
        def count(table: str) -> int:
            return int(self._conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"])

        return {
            "schema_version": SCHEMA_VERSION,
            "files": count("files"),
            "entities": count("entities"),
            "edges": count("edges"),
            "metrics": count("metrics"),
            "file_metrics": count("file_metrics"),
            "symbols": count("symbols"),
            "db_path": str(self.path),
        }

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> GraphStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _entity_from_row(row: sqlite3.Row) -> Entity:
    return Entity(
        id=row["id"],
        file_path=row["file_path"],
        kind=EntityKind(row["kind"]),
        name=row["name"],
        qualified_name=row["qualified_name"],
        start_byte=row["start_byte"],
        end_byte=row["end_byte"],
        start_line=row["start_line"],
        end_line=row["end_line"],
        parent_id=row["parent_id"],
        attrs=json.loads(row["attrs"]),
    )


def _edge_from_row(row: sqlite3.Row) -> Edge:
    return Edge(
        src_id=row["src_id"],
        kind=EdgeKind(row["kind"]),
        dst_id=row["dst_id"],
        dst_ref=row["dst_ref"],
        provenance=Provenance(row["provenance"]),
        resolution=Resolution(row["resolution"]),
        confidence=row["confidence"],
        attrs=json.loads(row["attrs"]),
    )


def entities_by_kind(entities: Iterable[Entity], kind: EntityKind) -> list[Entity]:
    return [e for e in entities if e.kind is kind]
