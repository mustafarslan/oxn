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

from oxn.graph.rows import drop_unresolved_references
from oxn.scip.aliases import AliasReport, resolve_import_aliases
from oxn.scip.index import load_index
from oxn.scip.join import join_document

if TYPE_CHECKING:  # pragma: no cover
    from oxn.graph.indexer import Indexer
    from oxn.scip.index import ScipIndex


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
    #: Why the rest were not joined, counted rather than recomputed by a script.
    #:
    #: The per-cause split behind every call-coverage figure OXN publishes used to be produced
    #: by an ad-hoc script and pasted into a docstring -- so when the receiver fallback went on
    #: 2026-09-10 and coverage fell, the headline was corrected and the causes beneath it were
    #: not, and could not be without re-running the corpora. A number the tool reports cannot
    #: go stale that way.
    calls_without_occurrence: int = 0
    calls_without_caller: int = 0
    #: Project files OXN parses that the SCIP index has no document for.
    #:
    #: **The scope of `call_coverage`'s denominator, which is otherwise invisible.** This loop
    #: walks the index's documents, so a file the indexer never covered contributes no call
    #: sites -- neither joined nor missed -- and the coverage figure is silently "of call sites
    #: in files the indexer saw". On `typescript-nest` that is 1,020 files of 1,913 and 8,184
    #: call sites of 44,548: the published 76.2% describes 18% of the tree. Nothing is wrong
    #: with the measurement -- you cannot join what was not indexed -- but a reader takes "76%
    #: of call sites" to mean the project's, and until this counter existed nothing said
    #: otherwise.
    unindexed_files: int = 0
    definition_sites: int = 0
    joined_definition_sites: int = 0
    seconds: float = 0.0
    skipped: list[str] = field(default_factory=list)
    #: Documents `oxn.yaml`'s `exclude` names. Counted rather than listed: on OXN's own
    #: tree that is 1,464 corpus files, and a list of them is not a report.
    excluded: int = 0
    #: `REFERENCES` edges naming a symbol outside the tree, discarded after resolution.
    #: See `graph.rows.drop_unresolved_references` for why these and not unresolved calls.
    dropped_references: int = 0
    #: Calls made through an imported name, which SCIP leaves as a document-local symbol.
    #: See `scip.aliases`.
    aliases: AliasReport = field(default_factory=lambda: AliasReport())

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
            "calls_without_occurrence": self.calls_without_occurrence,
            "calls_without_caller": self.calls_without_caller,
            "unindexed_files": self.unindexed_files,
            "definition_coverage": round(self.definition_coverage, 4),
            "seconds": round(self.seconds, 2),
            "skipped": self.skipped[:20],
            "excluded": self.excluded,
            "dropped_references": self.dropped_references,
            "import_aliases": self.aliases.as_dict(),
        }


def ingest_index(indexer: Indexer, index_path: Path | str) -> IngestReport:
    """Read a ``.scip`` file and merge it into the graph.

    Files the index names but OXN has no profile for are skipped and reported: a SCIP index
    covers whatever its language server saw, which is not always the same set -- and that
    includes files `oxn.yaml` excludes, which are counted and dropped rather than written.
    """
    from oxn.graph.builder import build_file, content_sha
    from oxn.graph.sources import _out_of_scope
    from oxn.languages import get_parser
    from oxn.profiles import profile_for_path

    started = time.perf_counter()
    index = load_index(index_path)
    report = IngestReport(documents=len(index.documents))

    for document in index.documents:
        relative = document.relative_path
        source_path = indexer.root / relative
        # A SCIP indexer walks the tree its own way and knows nothing of `oxn.yaml`, so it
        # returns documents the project has excluded. Writing them made `oxn index` and
        # `oxn check` disagree about what the cache holds -- `oxn index .` put 1,464
        # benchmark-corpus files into OXN's own cache, which `tests/test_rules_corpus.py`
        # forbids precisely because other people's code is not this project's debt.
        if _out_of_scope(source_path, indexer.root, indexer.exclude):
            report.excluded += 1
            continue
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
        # index may cover files the caller never asked OXN to index -- but `put_file` is a
        # *replace*, and it takes that file's metrics with it. Writing over a row that is
        # already current and measured is how `oxn index` blinded the gate: 0 metric rows on
        # httpx, and `oxn check .` reporting "60 files, 0 violations, passed" on a corpus
        # with 118 of them. `is_current(measured=True)` is the guard that made it visible;
        # this is the one that stops it happening.
        if not indexer.store.is_current(
            relative, content_sha(source), profile.version, indexer.grammar_version(), measured=True
        ):
            indexer.store.put_file(parsed, profile.version, indexer.grammar_version())
        indexer.store.put_symbols(relative, result.definitions)
        indexer.store.put_edges(relative, result.edges)

        report.matched_documents += 1
        report.symbols += len(result.definitions)
        report.edges += len(result.edges)
        report.call_sites += result.call_sites
        report.joined_call_sites += result.joined_call_sites
        report.calls_without_occurrence += result.calls_without_occurrence
        report.calls_without_caller += result.calls_without_caller
        report.definition_sites += result.definition_sites
        report.joined_definition_sites += result.joined_definition_sites

    report.resolved_edges = indexer.store.resolve_edge_targets()
    # After SCIP's own resolution, never instead of it: a target SCIP can name is compiler
    # evidence, and only what is left over is worth asking a name resolver about.
    report.aliases = resolve_import_aliases(indexer)
    report.resolved_edges += report.aliases.resolved
    report.dropped_references = drop_unresolved_references(indexer.store)
    report.unindexed_files = _unindexed(indexer, index)
    report.seconds = time.perf_counter() - started
    return report


def _unindexed(indexer: Indexer, index: ScipIndex) -> int:
    """How many of the project's own files the index has no document for.

    A walk of the tree, which ADR-0004 measured at 21 ms over nest's 1,913 files -- affordable
    once per ingest, and the only way to say what `call_coverage`'s denominator leaves out.
    """
    covered = {document.relative_path for document in index.documents}
    return sum(
        1 for path in indexer.sources([indexer.root]) if indexer.relative(path) not in covered
    )
