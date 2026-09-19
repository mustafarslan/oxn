"""Recover a call whose SCIP target is a document-local binding of an import.

**A call through an imported name is how Python crosses a file boundary, and SCIP hands it
back unresolved.** `scip-python` binds `from oxn.check import _measure` as `local 2`, the call
site resolves to that same `local 2`, and a local is document-scoped by the specification
(`scip.join.scoped`) -- so the edge names a symbol nothing in the tree defines. On OXN's own
source that is **1,712 of 8,671 call edges**.

The index does not carry the answer. Both occurrences of `local 2` -- the one on the import
line and the one at the call -- are plain reads: no definition role, no relationships, and
nothing tying the local to the symbol it aliases. The module symbol on the import line is no
help either, because an editable install makes it `` `oxn.check` `` while every definition in
the index is `` `src.oxn.check` ``, a prefix nothing defines.

So the answer comes from OXN's own L1 resolver, which already knows how to follow an import to
a file and a name to a declaration, and already reports how sure it is. **This is the rung
change made explicit**: the call site is SCIP's (`Provenance.SCIP`), the target is L1's
(`Resolution.L1`), and `metrics.callgraph.build_call_graph` admits an L1 edge only at
confidence 1.0 -- so an ambiguous target is counted as declined rather than believed.

**Two gates, because a local is not always an import.** Measured over OXN's 2,197 calls that
resolve to a document-local: 44.6% are bare names bound by an import, 33.8% are qualified
callees whose receiver has no known type, 20.3% are builtins, and **0.5% are a local variable
holding a callable**. That last group is the dangerous one: `handler = lambda: 1; handler()`
would otherwise be answered by "a unique declaration anywhere in the project" and reach an
unrelated `handler` in another file -- a fabricated edge of exactly the shape `scip.join.scoped`
records. The binding at the call site must be an import, and the name must not be assigned
anywhere in the file, which is the rebinding case `from m import f; f = wrap(f)`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from oxn.graph.model import EdgeKind, Resolution

if TYPE_CHECKING:  # pragma: no cover
    from oxn.graph.indexer import Indexer
    from oxn.resolve.scopes import ScopeTree
    from oxn.resolve.symbols import ProjectSymbols


@dataclass
class AliasReport:
    """What one pass recovered, and what it refused to."""

    #: Edges whose target the resolver named with certainty. These enter the call graph.
    resolved: int = 0
    #: Named, but with more than one candidate, so **not written**: see `_Pass.decide`.
    ambiguous: int = 0
    #: The name at the call site is not an import binding -- a local holding a callable, a
    #: parameter, a builtin, or a receiver whose type is unknown.
    not_an_import: int = 0
    #: An import binding whose name is also assigned in the file, so the call may reach the
    #: assignment rather than the import.
    rebound: int = 0
    #: An import binding the resolver could not place: the specifier left the tree.
    unresolved: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "resolved": self.resolved,
            "ambiguous": self.ambiguous,
            "not_an_import": self.not_an_import,
            "rebound": self.rebound,
            "unresolved": self.unresolved,
        }


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One unresolved call edge through a document-local, and what is known about it."""

    rowid: int
    path: str
    name: str
    at: int


def resolve_import_aliases(indexer: Indexer) -> AliasReport:
    """Fill in the targets of calls made through an imported name. See the module docstring."""
    from oxn.graph.depgraph import build_dependency_graph
    from oxn.resolve.project import build_class_views
    from oxn.resolve.symbols import build_project_symbols

    candidates = _candidates(indexer)
    if not candidates:
        return AliasReport()

    files = indexer.sources([indexer.root])
    views = build_class_views(indexer, files)
    graph = build_dependency_graph(indexer.root, files)
    run = _Pass(
        indexer=indexer,
        scopes=views.scopes,
        symbols=build_project_symbols(views.entities, views.scopes, graph),
    )
    for candidate in candidates:
        run.decide(candidate)
    return run.report


@dataclass
class _Pass:
    """State the pass carries across candidates: the tree's views, and what it has decided."""

    indexer: Indexer
    #: file -> its `ScopeTree`, from `resolve.project.build_class_views`.
    scopes: dict[str, ScopeTree]
    symbols: ProjectSymbols
    report: AliasReport = field(default_factory=AliasReport)

    def decide(self, candidate: _Candidate) -> None:
        """Resolve one call through a document-local, or record why it was refused."""
        scopes = self.scopes.get(candidate.path)
        binding = scopes.resolve(candidate.name, candidate.at) if scopes else None
        if scopes is None or binding is None or binding[0].kind != "import":
            self.report.not_an_import += 1
            return
        if candidate.name in scopes.assigned:
            # `from m import f` and then `f = wrap(f)`: the call may reach the wrapper. The
            # scopes cannot answer this from a lookup -- `Scope.declare` keeps the first
            # binding, so the import hides the assignment -- which is why `ScopeTree` records
            # assigned names separately. Fired 0 times over OXN's 990 candidates; kept
            # because the failure mode is a confidently wrong edge, not a missing one.
            self.report.rebound += 1
            return

        found = self.symbols.resolve_call(candidate.path, candidate.name)
        if found is None:
            self.report.unresolved += 1
            return
        if not found.is_certain:
            # Counted and *not written*. `ResolvedName` carries `1/n` so an ambiguous answer
            # can be recorded honestly, and `build_call_graph` would decline it -- but a row
            # with `dst_id` set is fact-shaped, and 468 of these on `typescript-nest` each
            # naming one of several same-named declarations is 468 probably-wrong targets
            # sitting in the cache for the next reader who forgets to check `confidence`.
            # Nothing consumes them today, so writing them buys nothing and risks that.
            self.report.ambiguous += 1
            return
        self.report.resolved += 1
        _write(self.indexer, candidate.rowid, found.entity_id, found.confidence)


def _candidates(indexer: Indexer) -> list[_Candidate]:
    """Unresolved call edges through a document-local that carry a name to look up.

    `scip.join._call_attrs` records the name and byte offset for exactly these rows; a row
    without them predates that and is left alone rather than guessed at. A qualified name is
    skipped here rather than in `decide`: the binding that matters for `a.b()` is `a`'s type,
    which no import table answers.
    """
    found = indexer.store._conn.execute(  # noqa: SLF001
        "SELECT rowid, file_path, attrs FROM edges"
        " WHERE kind = ? AND dst_id IS NULL AND dst_ref LIKE 'local %'",
        (EdgeKind.CALLS.value,),
    ).fetchall()
    out: list[_Candidate] = []
    for row in found:
        attrs = json.loads(row["attrs"] or "{}")
        name, at = attrs.get("name"), attrs.get("at")
        if isinstance(name, str) and name and isinstance(at, int) and "." not in name:
            out.append(_Candidate(row["rowid"], row["file_path"], name, at))
    return out


def _write(indexer: Indexer, rowid: int, entity_id: str, confidence: float) -> None:
    """Point the row at the entity, and say which rung named it.

    `resolution` moves to L1 while `provenance` stays `scip`, because the two answer different
    questions: SCIP found the call, the name resolver found what it calls. `docs/metrics.md`
    section 2.3 promises exactness per rung, so a row that quietly kept L2 would be claiming
    compiler-grade evidence for a name-based answer.
    """
    with indexer.store.transaction() as conn:
        conn.execute(
            "UPDATE edges SET dst_id = ?, resolution = ?, confidence = ?,"
            " attrs = json_set(attrs, '$.via', 'import_alias') WHERE rowid = ?",
            (entity_id, Resolution.L1.value, confidence, rowid),
        )
