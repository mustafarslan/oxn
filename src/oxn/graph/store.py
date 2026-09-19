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
    Entity,
    EntityMetrics,
    MetricValue,
    ParsedFile,
    Resolution,
)
from oxn.graph.rows import entity_from_row

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Collection, Iterator

#: Bump on any change to the statements below. A mismatch rebuilds the cache.
#:
#: **Also bump when the metric engine starts emitting a new key.** The per-file cache key is
#: `(path, content_sha, profile_version, grammar_version, schema_version)` and a new metric
#: changes none of the first four, so an unchanged file keeps rows that predate the metric and
#: the gate sees nothing. That is how `nom` and `wmc` first landed: three rows in the whole
#: repository, one file, and a clean `oxn check --deep` that had checked almost nothing.
#:
#: **And when the builder starts recording an entity it used to miss, or labelling one
#: differently**, for the same reason: `graph.builder` is not in the key either. Version 7 is
#: the curried-callable fix -- a callable whose body *is* another callable yielded one entity
#: where it should yield two. Version 8 labels Go's `func_literal` and Rust's
#: `closure_expression` as lambdas rather than as functions. Version 9 records
#: `attrs["implements"]`, which is what lets a Rust type's `impl` blocks total together.
#: Version 10 records a Go interface's `method_spec`s, which were no entity at all, so every
#: Go interface measured NOM 0. Version 11 labels those as methods rather than functions,
#: without which the gate still read NOM 0, and labels a Go interface `interface`.
#: Version 12 names a class-field callable and labels it a method -- `handler = (x) => x`
#: is how a TypeScript or JavaScript class writes one, and reading it as an anonymous
#: lambda made both class ceilings inert against the shape. Version 13 labels Java's
#: `lambda_expression` and ECMAScript's `generator_function` as lambdas, the last two
#: `function_like` kinds that can carry no name and were counted as named functions.
#: Version 15 is a *metric* change of the kind the paragraph above is about: `shredding`
#: fired in two of six languages and now fires in all six, so a cached `shredding_cluster`
#: row from before it predates the rule that produced it.
#: Version 14 records every name a Go `parameter_declaration` declares: `f(a, b, c int)`
#: groups three parameters under one node, and `attrs["parameters"]` held one of them.
SCHEMA_VERSION = 15

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
    """A code-graph cache backed by one SQLite file.

    **Wide on purpose, and baselined rather than split.** This class sits above both class
    ceilings, which is what those ceilings are for -- but the count is the trigger and not
    the verdict. Measured, it is a query catalogue rather than an accumulation of
    responsibilities: LCOM4 reads 2, and the second component is `__enter__`, a one-line
    `return self` that touches nothing. Every other method reaches `self._conn`, directly or
    through `transaction`.

    So there is no seam here to cut along. Partitioning by subject would produce parts that
    all share the one connection, and a facade over them returns the same method count with
    a layer of delegation added. `.oxn/baseline.json` holds the number instead, which is the
    ratchet doing its job: accepted, and it may not grow. If it does grow, check LCOM4 first
    -- a rise there is a second responsibility arriving, and *that* is worth splitting.
    `tests/test_wide_repository.py` pins both halves of this.
    """

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
        self,
        path: str,
        content_sha: str,
        profile_version: int,
        grammar_version: str,
        *,
        measured: bool = False,
    ) -> bool:
        """True if this file's stored graph was built from exactly this input.

        ``measured`` also requires the file to *have* measurements. "Parsed" and "measured"
        were one question here and are two facts: `scip.ingest` writes a file row directly --
        it needs one before symbols and edges can reference it -- and `put_file` replaces the
        row, taking that file's metrics with it. The row left behind carries the current sha,
        so this said current and nothing measured the file again. **Measured on
        `python-httpx`: after `oxn index --index-file`, 0 metric rows, and `oxn check .`
        reported "60 files, 0 violations, passed" on a corpus with violations in it.**
        """
        row = self._conn.execute(
            "SELECT content_sha, profile_version, grammar_version FROM files WHERE path = ?",
            (path,),
        ).fetchone()
        if row is None:
            return False
        stamp = (row["content_sha"], row["profile_version"], row["grammar_version"])
        if stamp != (content_sha, profile_version, grammar_version):
            return False
        # Inlined: `GraphStore` is at its baselined NOM and the ratchet is not spent here.
        return not measured or _has_measurements(self._conn, path)

    def put_file(self, parsed: ParsedFile, profile_version: int, grammar_version: str) -> None:
        """Replace one file's contribution atomically."""
        with self.transaction() as conn:
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
        with self.transaction() as conn:
            conn.execute("DELETE FROM symbols WHERE file_path = ?", (path,))
            conn.executemany(
                "INSERT OR REPLACE INTO symbols (symbol, entity_id, file_path) VALUES (?, ?, ?)",
                [(symbol, entity_id, path) for symbol, entity_id in symbols.items()],
            )

    def put_edges(self, path: str, edges: list[Edge]) -> None:
        """Replace the non-containment edges belonging to one file."""
        with self.transaction() as conn:
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
        with self.transaction() as conn:
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
        with self.transaction() as conn:
            conn.execute("DELETE FROM metrics WHERE file_path = ?", (path,))
            conn.executemany(
                "INSERT INTO metrics (entity_id, metric_key, value, exactness, resolution,"
                " bound, explanation, file_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def put_file_metrics(self, values: dict[str, dict[str, float]]) -> None:
        """Replace file-scoped measurements for the paths named."""
        with self.transaction() as conn:
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
        return [entity_from_row(row) for row in rows]

    def forget(self, path: str) -> None:
        """Drop a file that no longer exists in the working tree."""
        with self.transaction() as conn:
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
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A scoped write: commit on success, roll back on any exception.

        **Public, because two callers outside this class already needed it** and were
        reaching past the underscore with `# noqa: SLF001` to get it -- `scip.aliases`
        retargeting an edge after alias resolution, and `graph.rows` dropping unresolved
        references. Both are single-statement edge mutations, and the honest options were
        to add a method per statement or to admit that handing out a scoped transaction is
        part of what a repository does. The first would have taken NOM from 26 to 28,
        against a baseline recorded at 26 -- the ratchet refusing an encapsulation
        improvement, which is worth noticing about a ratchet.

        So the surface is declared rather than leaked. A caller holding this is inside the
        store's transaction, not around it: do not nest, and do not hold it across a read
        that another writer could interleave with.
        """
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


def incomplete_paths(store: GraphStore) -> set[str]:
    """Paths whose stored tree had an error node.

    `parse_incomplete` was persisted from the first schema and read back by nothing, so a file
    that failed to parse was reported on the run that parsed it and never again: the second
    `oxn check` came back clean because the row was a cache hit.

    A module function rather than a method, and the gate is what said so. `GraphStore` is
    baselined at 26 methods against a ceiling of 12, and the baseline may not grow -- adding a
    27th made `methods_per_class` and `weighted_methods_per_class` regress on the commit that
    added it. The rule was right: a read-only query needs nothing from the class but its
    connection, and hanging it off a type already twice over its ceiling is how that number
    got there. One statement for the whole tree, not one per file, so a healthy run pays a
    single query to find a case it almost never has.
    """
    rows = store._conn.execute("SELECT path FROM files WHERE parse_incomplete = 1")  # noqa: SLF001
    return {str(row[0]) for row in rows}


def _has_measurements(conn: sqlite3.Connection, path: str) -> bool:
    """Whether anything in this file has been measured at all. Module-level: see `rows.py`."""
    found = conn.execute(
        "SELECT 1 FROM metrics m JOIN entities e ON e.id = m.entity_id WHERE e.file_path = ?"
        " LIMIT 1",
        (path,),
    ).fetchone()
    return found is not None
