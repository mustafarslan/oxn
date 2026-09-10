"""Turning stored rows back into the graph, and the one read that has no owner.

Split out of `store.py` on 2026-09-10 for a reason the gate stated plainly: that module was
at `file_sloc` 495 of 500 and `GraphStore` at its baselined NOM and WMC, so a single new
query broke two ceilings at once. Trimming prose would have bought a few lines and left the
next reader with the same problem; what these three have in common is that they are about
*rows*, not about the connection, so they are a module.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from oxn.graph.model import EdgeKind, Entity, EntityKind

if TYPE_CHECKING:  # pragma: no cover
    import sqlite3

    from oxn.graph.store import GraphStore


def entity_from_row(row: sqlite3.Row) -> Entity:
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


def call_edges(store: GraphStore) -> list[tuple[str, str | None, str, float]]:
    """Every call edge, as `build_call_graph` wants them -- **including the unresolved**.

    Filtering `dst_id IS NOT NULL` in SQL is the tempting query and the wrong one:
    `build_call_graph` counts a call to nothing as `unresolved_targets`, and that number is
    what tells a reader how much of the picture they are being shown. 1,939 of httpx's 4,025
    call edges leave the tree, and the filter renders that as `0 declined`.

    **Rows exist only after a SCIP index is ingested**: nothing at L0/L1 writes a `calls`
    edge (ADR-0002), so an unindexed tree returns nothing at all -- which is why `oxn calls`
    answers `UNAVAILABLE` rather than reporting an empty call graph.
    """
    # `_conn` from outside the class, and the alternative is worse: a public `connection`
    # accessor is one more method on a class the baseline has pinned at NOM 26, and exposing
    # a raw sqlite handle is a wider API than one query needs. This module is `store.py`'s
    # other half -- it was one file until the ceiling said otherwise.
    found = store._conn.execute(  # noqa: SLF001
        "SELECT src_id, dst_id, resolution, confidence FROM edges WHERE kind = ?",
        (EdgeKind.CALLS.value,),
    ).fetchall()
    return [
        (row["src_id"], row["dst_id"], row["resolution"], float(row["confidence"]))
        for row in found
    ]
