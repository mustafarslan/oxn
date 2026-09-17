"""Typed records for a SCIP index, decoded from the wire format.

Field numbers below are from ``scip.proto`` and were **verified against a real index**
produced by ``scip-python`` 0.6.6 rather than taken from documentation -- the same practice
that caught three wrong assumptions about tree-sitter grammars in earlier phases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from oxn.scip.wire import iter_fields, read_packed_varints, text

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator

# --- Index ---
_DOCUMENTS = 2
_EXTERNAL_SYMBOLS = 3
# --- Document ---
_RELATIVE_PATH = 1
_OCCURRENCES = 2
_SYMBOLS = 3
# --- Occurrence ---
_RANGE = 1
_SYMBOL = 2
_SYMBOL_ROLES = 3
# --- SymbolInformation ---
_INFO_SYMBOL = 1
_RELATIONSHIPS = 4
_KIND = 5
_DISPLAY_NAME = 6
# --- Relationship ---
_REL_SYMBOL = 1
_IS_IMPLEMENTATION = 3
_IS_TYPE_DEFINITION = 4

#: ``symbol_roles`` is a bitfield. Definition is the bit that maps an occurrence to the
#: place a symbol is *declared*, which is what the range join needs.
ROLE_DEFINITION = 0x1
ROLE_IMPORT = 0x2
ROLE_WRITE = 0x4
ROLE_READ = 0x8


@dataclass(frozen=True, slots=True)
class ScipOccurrence:
    """One symbol reference at a source position.

    Positions are **0-based** and character-oriented, matching tree-sitter's ``start_point``.
    """

    symbol: str
    start_line: int
    start_char: int
    end_line: int
    end_char: int
    roles: int

    @property
    def is_definition(self) -> bool:
        return bool(self.roles & ROLE_DEFINITION)

    @property
    def is_import(self) -> bool:
        return bool(self.roles & ROLE_IMPORT)


@dataclass(frozen=True, slots=True)
class ScipRelationship:
    """A typed link between symbols. ``is_implementation`` gives inheritance directly."""

    symbol: str
    is_implementation: bool = False
    is_type_definition: bool = False


@dataclass(frozen=True, slots=True)
class ScipSymbol:
    """What the indexer knows about one declared symbol."""

    symbol: str
    display_name: str = ""
    kind: int = 0
    relationships: tuple[ScipRelationship, ...] = ()


@dataclass
class ScipDocument:
    """One indexed file."""

    relative_path: str
    occurrences: list[ScipOccurrence] = field(default_factory=list)
    symbols: list[ScipSymbol] = field(default_factory=list)

    def definitions(self) -> Iterator[ScipOccurrence]:
        return (occurrence for occurrence in self.occurrences if occurrence.is_definition)


@dataclass
class ScipIndex:
    """A parsed SCIP index."""

    documents: list[ScipDocument] = field(default_factory=list)
    external_symbols: list[ScipSymbol] = field(default_factory=list)
    #: Documents dropped because another already covered that path.
    #:
    #: **One index can describe a file twice.** A SCIP indexer is given *projects*, not files,
    #: and a file included by two of them is emitted once per project -- `scip-typescript`
    #: over `tsconfig.json` and `tsconfig.spec.json` produces 1,840 documents for 1,444 paths
    #: on `typescript-nest`, because both configs include `integration/**`. The store is safe
    #: either way (`put_symbols` and `put_edges` replace by path), but every counter on
    #: `IngestReport` accumulates per document, so the copies would inflate `call_sites` and
    #: `symbols` for 396 files while the ratios quietly stayed plausible.
    #:
    #: Kept as a number rather than dropped in silence: it says a project's configs overlap,
    #: which is a fact about that project and not a fault. On nest's 396 the two copies carry
    #: identical occurrence counts, so which one survives does not matter -- the first does,
    #: because that is reproducible.
    duplicate_documents: int = 0

    @property
    def occurrence_count(self) -> int:
        return sum(len(document.occurrences) for document in self.documents)

    def by_path(self) -> dict[str, ScipDocument]:
        return {document.relative_path: document for document in self.documents}


def load_index(path: Path | str) -> ScipIndex:
    """Parse a ``.scip`` file."""
    return parse_index(Path(path).read_bytes())


def parse_index(data: bytes) -> ScipIndex:
    """Parse a SCIP index, keeping one document per path. See `ScipIndex.duplicate_documents`."""
    index = ScipIndex()
    seen: set[str] = set()
    for number, _, value in iter_fields(data):
        if number == _DOCUMENTS and isinstance(value, bytes):
            document = _parse_document(value)
            if document.relative_path in seen:
                index.duplicate_documents += 1
                continue
            seen.add(document.relative_path)
            index.documents.append(document)
        elif number == _EXTERNAL_SYMBOLS and isinstance(value, bytes):
            index.external_symbols.append(_parse_symbol(value))
    return index


def _parse_document(data: bytes) -> ScipDocument:
    document = ScipDocument(relative_path="")
    for number, _, value in iter_fields(data):
        if number == _RELATIVE_PATH and isinstance(value, bytes):
            document.relative_path = text(value)
        elif number == _OCCURRENCES and isinstance(value, bytes):
            occurrence = _parse_occurrence(value)
            if occurrence is not None:
                document.occurrences.append(occurrence)
        elif number == _SYMBOLS and isinstance(value, bytes):
            document.symbols.append(_parse_symbol(value))
    return document


def _parse_occurrence(data: bytes) -> ScipOccurrence | None:
    """One occurrence: a symbol, where it appears, and the role it plays there."""
    span, symbol, roles = _occurrence_fields(data)
    if not symbol or not span:
        return None
    bounds = _span_bounds(span)
    if bounds is None:  # pragma: no cover - malformed
        return None
    start_line, start_char, end_line, end_char = bounds
    return ScipOccurrence(
        symbol=symbol,
        start_line=start_line,
        start_char=start_char,
        end_line=end_line,
        end_char=end_char,
        roles=roles,
    )


def _occurrence_fields(data: bytes) -> tuple[list[int], str, int]:
    """The three fields an occurrence carries, in whichever order they were written."""
    span: list[int] = []
    symbol = ""
    roles = 0
    for number, _wire_type, value in iter_fields(data):
        if number == _RANGE:
            if isinstance(value, bytes):
                span = read_packed_varints(value)
            elif isinstance(value, int):
                span.append(value)  # unpacked encoding, still legal
        elif number == _SYMBOL and isinstance(value, bytes):
            symbol = text(value)
        elif number == _SYMBOL_ROLES and isinstance(value, int):
            roles = value
    return span, symbol, roles


def _span_bounds(span: list[int]) -> tuple[int, int, int, int] | None:
    """A range is 3 elements when it stays on one line and 4 when it spans lines.

    Handling only the 4-element form silently drops most occurrences in real code.
    """
    if len(span) == 3:
        start_line, start_char, end_char = span
        return start_line, start_char, start_line, end_char
    if len(span) >= 4:
        start_line, start_char, end_line, end_char = span[:4]
        return start_line, start_char, end_line, end_char
    return None


def _parse_symbol(data: bytes) -> ScipSymbol:
    symbol = ""
    display_name = ""
    kind = 0
    relationships: list[ScipRelationship] = []
    for number, _, value in iter_fields(data):
        if number == _INFO_SYMBOL and isinstance(value, bytes):
            symbol = text(value)
        elif number == _DISPLAY_NAME and isinstance(value, bytes):
            display_name = text(value)
        elif number == _KIND and isinstance(value, int):
            kind = value
        elif number == _RELATIONSHIPS and isinstance(value, bytes):
            relationships.append(_parse_relationship(value))
    return ScipSymbol(symbol, display_name, kind, tuple(relationships))


def _parse_relationship(data: bytes) -> ScipRelationship:
    symbol = ""
    is_implementation = False
    is_type_definition = False
    for number, _, value in iter_fields(data):
        if number == _REL_SYMBOL and isinstance(value, bytes):
            symbol = text(value)
        elif number == _IS_IMPLEMENTATION and isinstance(value, int):
            is_implementation = bool(value)
        elif number == _IS_TYPE_DEFINITION and isinstance(value, int):
            is_type_definition = bool(value)
    return ScipRelationship(symbol, is_implementation, is_type_definition)
