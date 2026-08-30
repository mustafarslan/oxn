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


# ---- measurements -----------------------------------------------------------------------
#
# `MetricValue` and `EntityMetrics` live here rather than beside the engine that produces
# them, because they are *vocabulary* rather than analysis: the store persists them, the gate
# reads them, and the engine happens to be what fills them in. While they lived in
# `metrics.engine`, `graph/store.py` had to import the analysis layer to describe its own
# rows -- which OXN's own layer contract reported, correctly, as the graph reaching upwards.


@dataclass(frozen=True, slots=True)
class MetricValue:
    """One measurement, with everything needed to judge how much to trust it."""

    key: str
    value: float
    exactness: str = "EXACT"
    resolution: Resolution = Resolution.L0
    #: Human-readable trail, where the metric can produce one.
    explanation: tuple[str, ...] = ()
    #: ``"exact"`` | ``"lower"`` | ``"upper"``. An approximate value is still useful when
    #: its direction of error is known: a *lower bound* that already exceeds a ceiling
    #: proves the true value does too, so a ceiling gate can act on it soundly even though
    #: the number itself is not final. Without this, "only EXACT may block" would disable
    #: the project's headline gate for every function that makes a call.
    bound: str = "exact"

    @property
    def can_block_ceiling(self) -> bool:
        """Whether a "must not exceed" gate may act on this value."""
        return self.exactness == "EXACT" or self.bound == "lower"

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "value": self.value,
            "exactness": self.exactness,
            "resolution": self.resolution.value,
            "bound": self.bound,
        }
        if self.explanation:
            payload["explanation"] = list(self.explanation)
        return payload


@dataclass
class EntityMetrics:
    """All measurements for one entity."""

    entity_id: str
    qualified_name: str
    kind: EntityKind
    line: int
    values: dict[str, MetricValue] = field(default_factory=dict)

    def add(self, value: MetricValue) -> None:
        self.values[value.key] = value

    def get(self, key: str) -> float | None:
        found = self.values.get(key)
        return found.value if found else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "qualified_name": self.qualified_name,
            "kind": self.kind.value,
            "line": self.line,
            "metrics": {key: value.as_dict() for key, value in self.values.items()},
        }
