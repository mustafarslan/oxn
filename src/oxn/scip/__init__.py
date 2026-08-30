"""SCIP index ingestion -- the L2 resolution rung (ADR-0002).

A SCIP index is a *static artifact on disk*, which is the decisive property: the hook is a
cold process, so consuming an index is a file read in milliseconds where a language server
would cost seconds of startup. That is why SCIP is OXN's primary precision layer and LSP is
only a fallback.
"""

from oxn.scip.index import ScipDocument, ScipIndex, ScipOccurrence, ScipSymbol, load_index
from oxn.scip.runner import IndexerNotFound, available_indexers, run_indexer

__all__ = [
    "IndexerNotFound",
    "ScipDocument",
    "ScipIndex",
    "ScipOccurrence",
    "ScipSymbol",
    "available_indexers",
    "load_index",
    "run_indexer",
]
