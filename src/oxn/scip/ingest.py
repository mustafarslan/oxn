"""Ingest a SCIP index into the code graph.

Two passes, and the second is not optional. An edge frequently points at a symbol declared
in a file that had not been read yet when the edge was created, so targets are resolved
once every document has contributed its definitions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from oxn.scip.index import load_index
from oxn.scip.join import join_document

if TYPE_CHECKING:  # pragma: no cover
    from oxn.graph.indexer import Indexer


@dataclass
class IngestReport:
    """What an ingest achieved, in the terms the exit criterion is stated in."""

    documents: int = 0
    matched_documents: int = 0
    symbols: int = 0
    edges: int = 0
    resolved_edges: int = 0
    call_sites: int = 0
    joined_call_sites: int = 0
    definition_sites: int = 0
    joined_definition_sites: int = 0
    seconds: float = 0.0
    skipped: list[str] = field(default_factory=list)

    @property
    def call_coverage(self) -> float:
        """Share of call sites the index could name a target for."""
        return self.joined_call_sites / self.call_sites if self.call_sites else 0.0

    @property
    def definition_coverage(self) -> float:
        return (
            self.joined_definition_sites / self.definition_sites if self.definition_sites else 0.0
        )

    @property
    def edge_resolution(self) -> float:
        """Share of edges whose target is a symbol defined inside the tree."""
        return self.resolved_edges / self.edges if self.edges else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "documents": self.documents,
            "matched_documents": self.matched_documents,
            "symbols": self.symbols,
            "edges": self.edges,
            "resolved_edges": self.resolved_edges,
            "edge_resolution": round(self.edge_resolution, 4),
            "call_coverage": round(self.call_coverage, 4),
            "definition_coverage": round(self.definition_coverage, 4),
            "seconds": round(self.seconds, 2),
            "skipped": self.skipped[:20],
        }


def ingest_index(indexer: Indexer, index_path: Path | str) -> IngestReport:
    """Read a ``.scip`` file and merge it into the graph.

    Files the index names but OXN has no profile for are skipped and reported: a SCIP index
    covers whatever its language server saw, which is not always the same set.
    """
    from oxn.graph.builder import build_file
    from oxn.languages import get_parser
    from oxn.profiles import profile_for_path

    started = time.perf_counter()
    index = load_index(index_path)
    report = IngestReport(documents=len(index.documents))

    for document in index.documents:
        relative = document.relative_path
        source_path = indexer.root / relative
        profile = profile_for_path(relative)
        if profile is None or not source_path.exists():
            report.skipped.append(relative)
            continue

        source = source_path.read_bytes()
        tree = get_parser(profile.name).parse(source)
        if tree.root_node.has_error:
            report.skipped.append(relative)
            continue

        parsed = build_file(relative, source, profile, tree.root_node)
        result = join_document(list(parsed.entities), profile, tree.root_node, document)

        # The file row must exist before symbols and edges can reference it, and a SCIP
        # index may cover files the caller never asked OXN to index.
        indexer.store.put_file(parsed, profile.version, indexer.grammar_version())
        indexer.store.put_symbols(relative, result.definitions)
        indexer.store.put_edges(relative, result.edges)

        report.matched_documents += 1
        report.symbols += len(result.definitions)
        report.edges += len(result.edges)
        report.call_sites += result.call_sites
        report.joined_call_sites += result.joined_call_sites
        report.definition_sites += result.definition_sites
        report.joined_definition_sites += result.joined_definition_sites

    report.resolved_edges = indexer.store.resolve_edge_targets()
    report.seconds = time.perf_counter() - started
    return report
