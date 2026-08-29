"""The OXN Code Graph -- the normalized representation every metric reads.

Nothing above this module touches tree-sitter. A metric is a pure function of the graph,
which is what lets one implementation serve every language (docs/metrics.md section 1).

**Scope note.** P1 builds the *containment skeleton*: files, modules, types, functions, and
the parent/child relation between them. The edge kinds for imports, calls, field access and
inheritance are declared here in full but are not populated until P4/P5. Declaring them now
means later phases add rows, not schema.

**Entity identity.** IDs are content-derived and *position-independent*: a function keeps
its id when code above it shifts. This deviates from the illustrative scheme in the design
notes, which included the byte range, for two reasons -- P5 joins these ids against SCIP
symbols, which are themselves position-independent, and P10's trend tracking needs an
entity to stay the same entity across commits.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EntityKind(str, Enum):
    """What an entity is. Language-neutral by construction.

    ``(str, Enum)`` rather than ``StrEnum``: the latter is 3.11+, and OXN supports 3.10.
    Always serialise via ``.value``.
    """

    FILE = "file"
    MODULE = "module"
    CLASS = "class"
    INTERFACE = "interface"
    FUNCTION = "function"
    METHOD = "method"
    LAMBDA = "lambda"


class EdgeKind(str, Enum):
    """Every relation the graph will ever hold.

    Only ``CONTAINS`` is populated in P1, and it is stored denormalized as
    :attr:`Entity.parent_id` rather than as rows, since the containment relation is a tree.
    The rest are declared so that P4 and P5 add rows to an existing table.
    """

    CONTAINS = "contains"
    IMPORTS = "imports"
    DEPENDS_ON = "depends_on"
    EXTENDS = "extends"
    IMPLEMENTS = "implements"
    OVERRIDES = "overrides"
    CALLS = "calls"
    READS = "reads"
    WRITES = "writes"
    REFERENCES = "references"
    CO_CHANGED = "co_changed"
    AUTHORED = "authored"


class Provenance(str, Enum):
    """How a fact was established. Travels with every edge and every metric value."""

    SYNTAX = "syntax"
    NAME_HEURISTIC = "name_heuristic"
    SCIP = "scip"
    LSP = "lsp"
    VCS = "vcs"


class Resolution(str, Enum):
    """The resolution rung that produced a fact (ADR-0002).

    Surfaced in every output so an agent is never told an L0 approximation is exact.
    """

    L0 = "L0"
    L1 = "L1"
    L2 = "L2"


def entity_id(path: str, kind: EntityKind, qualified_name: str, occurrence: int = 0) -> str:
    """A stable, deterministic id for an entity.

    ``occurrence`` disambiguates same-named siblings -- overloads, or two lambdas on one
    line -- and is assigned in source order by the builder.
    """
    raw = f"{path}\0{kind.value}\0{qualified_name}\0{occurrence}"
    return hashlib.blake2b(raw.encode(), digest_size=16).hexdigest()


@dataclass(frozen=True, slots=True)
class Entity:
    """One node of the code graph."""

    id: str
    file_path: str
    kind: EntityKind
    name: str | None
    qualified_name: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    parent_id: str | None = None
    #: Kind-specific detail: parameters, async-ness, decorators, abstractness.
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1


@dataclass(frozen=True, slots=True)
class Edge:
    """One relation. Unpopulated in P1 apart from the declared schema."""

    src_id: str
    kind: EdgeKind
    dst_id: str | None = None
    #: Raw, unresolved target -- an import specifier, a callee name. Never dropped:
    #: docs/metrics.md section 4.1 makes silent dropping a false-negative machine.
    dst_ref: str | None = None
    provenance: Provenance = Provenance.SYNTAX
    resolution: Resolution = Resolution.L0
    confidence: float = 1.0
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ParsedFile:
    """The graph contribution of exactly one file."""

    path: str
    language: str
    content_sha: str
    size_bytes: int
    #: True when the tree contained ERROR or MISSING nodes. Metrics derived from an
    #: incomplete parse are suppressed rather than reported wrong (docs/metrics.md 9.4).
    parse_incomplete: bool
    entities: tuple[Entity, ...]
    edges: tuple[Edge, ...] = ()

    @property
    def root(self) -> Entity:
        return self.entities[0]
