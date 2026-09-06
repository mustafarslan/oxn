"""Measure L0/L1 against L2 ground truth.

This module is the *reason* L2 was built first. A resolver that says "approximate" without
a number is asking to be trusted; one that publishes its precision and recall per language
can be reasoned about -- and its metrics can be gated on, or not, accordingly (ADR-0002).

Definitions used here, stated because "precision" is meaningless without them:

* the population is **call sites SCIP resolved to an in-tree definition, where SCIP's symbol
  names the same thing the source literally writes**. The second condition is not pedantry:
  ``scip-python`` 0.6.6 mis-resolves names re-exported through a package ``__init__.py``,
  landing on an *alphabetically adjacent* name -- ``Client`` becomes ``AsyncClient``,
  ``Response`` becomes ``Request``, ``wait_for`` becomes ``sleep``. That is 19% of non-local
  call sites in httpx. Grading against a ground truth that is wrong one time in five
  produces a number that measures the oracle's bugs, not ours, so those sites are excluded
  and counted;
* **recall** is the share of those call sites where L0/L1 also produced an answer;
* **precision** is the share of *its answers* that name the same entity SCIP did.

A resolver that answers rarely but correctly has high precision and low recall; one that
guesses freely has the reverse. Both numbers are needed to know which.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path

    from tree_sitter import Node

    from oxn.graph.model import Entity
    from oxn.profiles.base import LanguageProfile
    from oxn.resolve.symbols import ProjectSymbols, ResolvedName
    from oxn.scip.index import ScipDocument, ScipOccurrence


@dataclass
class ResolutionAccuracy:
    """How well L0/L1 reproduce L2 on one corpus."""

    language: str
    graded_call_sites: int = 0
    answered: int = 0
    correct: int = 0
    #: Call sites dropped because the oracle's own symbol contradicts the source text.
    excluded_untrustworthy: int = 0
    #: Answers L1 gave with full confidence -- a unique candidate, not a guess among many.
    confident_answered: int = 0
    confident_correct: int = 0
    disagreements: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        """Share of gradeable call sites where L0/L1 produced any answer."""
        return self.answered / self.graded_call_sites if self.graded_call_sites else 0.0

    @property
    def precision(self) -> float:
        """Share of L0/L1's answers that match L2."""
        return self.correct / self.answered if self.answered else 0.0

    @property
    def f1(self) -> float:
        total = self.precision + self.recall
        return 2 * self.precision * self.recall / total if total else 0.0

    @property
    def confident_precision(self) -> float:
        """Precision restricted to answers L1 was certain of.

        This is the number that decides whether a metric may be *gated* on at L0/L1:
        overall precision is dragged down by low-confidence guesses, which a gate can simply
        decline to make.
        """
        return self.confident_correct / self.confident_answered if self.confident_answered else 0.0

    @property
    def confident_recall(self) -> float:
        return self.confident_answered / self.graded_call_sites if self.graded_call_sites else 0.0

    @property
    def excluded_share(self) -> float:
        population = self.graded_call_sites + self.excluded_untrustworthy
        return self.excluded_untrustworthy / population if population else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "language": self.language,
            "graded_call_sites": self.graded_call_sites,
            "excluded_untrustworthy": self.excluded_untrustworthy,
            "excluded_share": round(self.excluded_share, 4),
            "answered": self.answered,
            "correct": self.correct,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "confident_answered": self.confident_answered,
            "confident_precision": round(self.confident_precision, 4),
            "confident_recall": round(self.confident_recall, 4),
        }


@dataclass(frozen=True, slots=True)
class _Grading:
    """One file's grading run: the source of truth, the guesser, and the tally.

    All of it is invariant across every call site in the file; only the node being graded
    varies. Passing it as six separate arguments made a seven-parameter function whose
    signature said nothing about what grading *is*.
    """

    path: str
    profile: LanguageProfile
    document: ScipDocument
    symbols: ProjectSymbols
    #: SCIP symbol -> the entity id it names in our graph, when it names one at all.
    scip_to_entity: dict[str, str]
    accuracy: ResolutionAccuracy


def grade_file(grading: _Grading, tree_root: Node) -> None:
    """Compare L0/L1's answer to L2's for every call site in one file."""
    spec = grading.profile.metrics.cognitive
    if not spec.call_kinds:
        return

    by_position = {
        (occurrence.start_line, occurrence.start_char): occurrence
        for occurrence in grading.document.occurrences
    }

    stack = [tree_root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type in spec.call_kinds:
            _grade_call(grading, node, by_position)


def _grade_call(
    grading: _Grading, node: Node, by_position: dict[tuple[int, int], ScipOccurrence]
) -> None:
    """Grade one call site, or decline to.

    Most call sites are declined, and for different reasons: SCIP may not have placed the
    callee in-tree either, or the oracle's idea of what is written may not match the
    source. Only what survives all of that is scored.
    """
    # noqa: PLC2701 on the private import -- one join rule, in one place.
    from oxn.scip.join import _last_name_position, scoped  # noqa: PLC2701

    spec = grading.profile.metrics.cognitive
    callee = node.child_by_field_name(spec.callee_field)
    if callee is None:
        return

    occurrence = by_position.get(_last_name_position(callee))
    if occurrence is None:
        return
    truth = grading.scip_to_entity.get(scoped(occurrence.symbol, grading.path))
    if truth is None:
        return  # SCIP could not place it in-tree either; nothing to grade against

    name = _callee_name(callee)
    if not name:
        return

    # The oracle must agree with the source about *what is written* before it can arbitrate
    # what that name refers to. See the module docstring.
    if symbol_tail(occurrence.symbol) != name:
        grading.accuracy.excluded_untrustworthy += 1
        return

    grading.accuracy.graded_call_sites += 1
    guess = grading.symbols.resolve_call(
        grading.path, name, callee_qualifier(node, callee, grading.profile)
    )
    if guess is None:
        return

    grading.accuracy.answered += 1
    _score(grading, guess, truth, node, name)


def _score(grading: _Grading, guess: ResolvedName, truth: str, node: Node, name: str) -> None:
    """Tally one graded call site, recording the first few disagreements verbatim."""
    accuracy = grading.accuracy
    if guess.is_certain:
        accuracy.confident_answered += 1
    if guess.entity_id == truth:
        accuracy.correct += 1
        if guess.is_certain:
            accuracy.confident_correct += 1
    elif len(accuracy.disagreements) < 25:
        accuracy.disagreements.append(
            f"{grading.path}:{node.start_point[0] + 1} {name}(): "
            f"guessed {guess.qualified_name} [{guess.entity_id[:8]}] "
            f"truth {truth[:8]}"
        )


#: A SCIP symbol is five space-separated fields -- ``<scheme> <manager> <package> <version>
#: <descriptors>`` -- and only the fifth is a descriptor path. Splitting on ``/`` and ``#``
#: over the *whole* symbol worked by accident for Python and Go, whose package descriptor
#: always contains a ``/`` that happens to cut the prefix off. `rust-analyzer` writes
#: ``rust-analyzer cargo ripgrep 15.2.0 set_windows_exe_options().`` for a crate-level
#: function -- no ``/`` anywhere -- and the whole line came back as the "name".
_SYMBOL_FIELDS = 5

#: Descriptors are separated by ``/`` and ``#``; `rust-analyzer` additionally prefixes a
#: method with the impl block that owns it -- ``[Searcher]search_reader()``, and twice over
#: for a trait impl -- so ``]`` ends a descriptor too. The name is whatever follows the last
#: of these.
_DESCRIPTOR_BREAKS = "/#]"


def _descriptor_parts(descriptors: str) -> list[str]:
    """Split a descriptor path, respecting SCIP's backtick quoting.

    A descriptor is backtick-quoted **exactly when it contains a character that would
    otherwise be structural**, so splitting without tracking quotes cuts inside the one name
    that took the trouble to say it must not be cut. ripgrep has both shapes:
    ``[StderrReader]`r#async`()`` is a raw identifier holding a ``#``, and a trait impl for
    ``[u8; 4]`` would hold a ``]``.
    """
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    for char in descriptors:
        if char == "`":
            quoted = not quoted
        if char in _DESCRIPTOR_BREAKS and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def symbol_tail(symbol: str) -> str:
    r"""The final descriptor name of a SCIP symbol.

    ``\`httpx._client\`/Client#get().`` yields ``get``; ``\`httpx._urls\`/URLPattern#``
    yields ``URLPattern``.

    **This is a grader, so getting it wrong does not fail -- it silently narrows.** A tail
    that does not match the source text excludes the call site as untrustworthy, and until
    2026-09-06 this function mis-parsed every Rust symbol: 4,844 of ripgrep's 4,905
    exclusions were correct oracle answers thrown away, leaving Rust graded on 1,747 of
    6,652 call sites and a published 99.1% measured on a subset selected by a bug. The
    exclusion *rate* is now asserted per corpus in `test_oracle_scip.py`, because the rate is
    the only thing that was visibly wrong at the time and nothing was watching it.
    """
    fields = symbol.strip().split(" ", _SYMBOL_FIELDS - 1)
    descriptors = fields[-1] if len(fields) == _SYMBOL_FIELDS else symbol.strip()
    parts = [part for part in _descriptor_parts(descriptors) if part.strip(".:`() [")]
    if not parts:
        return ""
    return parts[-1].split("(")[0].rstrip(".:").strip("`[")


def callee_qualifier(node: Node, callee: Node, profile: LanguageProfile) -> str | None:
    """The name a call was written *through*, or ``None`` for a bare call.

    Two shapes, both read off the grammars rather than assumed. In Python, TypeScript, Go
    and Rust the callee node *is* the qualified expression -- `attribute`,
    `member_expression`, `selector_expression`, `field_expression` -- and the qualifier is
    its object field. Java is the exception: `method_invocation` keeps `object` and `name`
    as siblings, so the callee is a bare identifier and the qualifier hangs off the *call*.

    Rust's `std::fmt::format()` is deliberately not qualified here. Its callee is a
    `scoped_identifier`, not the receiver kind, and Rust is the one language where a path
    call and a receiver call are distinguishable by shape alone -- so it needs no table to
    tell them apart and gets the plain lookup.
    """
    spec = profile.metrics.scopes
    through = (
        callee.child_by_field_name(spec.attribute_object_field)
        if callee.type == spec.attribute_kind
        else None
    ) or node.child_by_field_name(spec.attribute_object_field)
    if through is None:
        return None
    return _head_name(through, profile.metrics.cognitive.call_kinds) or _text(through)


def _head_name(node: Node, call_kinds: frozenset[str]) -> str:
    """The **leftmost** name of a pure name chain -- the `a` of `a.b.c` -- or ``""``.

    The head is what the import table is keyed on: `import a` binds `a`, and `a.b.c()` is a
    call into `a` however long the chain. (`_callee_name` walks the other way, to the name
    being called; asking the table about that tail would answer "not an import" for every
    chained call.)

    **A call in the chain stops it.** `verify(repo).findByLastName()` is written through a
    *value* -- Mockito's `verify` is an external static import, but the mock it returns has
    an in-tree type, and the method really is ours. Treating `verify` as the qualifier
    declared that call external and cost exactly two graded answers on petclinic, which is
    how this clause came to exist.
    """
    current = node
    while True:
        if current.type in call_kinds:
            return ""
        if not current.named_child_count:
            return _text(current)
        current = current.named_children[0]


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace") if node.text else ""


def _callee_name(callee: Node) -> str:
    """The final name in a callee expression, which is the thing being called."""
    current = callee
    while current.named_child_count:
        current = current.named_children[-1]
    return current.text.decode("utf-8", "replace") if current.text else ""


def measure_corpus(root: Path, index_path: Path, language: str = "python") -> ResolutionAccuracy:
    """Grade L0/L1 against a SCIP index over a whole tree."""
    from oxn.graph.builder import build_file
    from oxn.graph.depgraph import build_dependency_graph
    from oxn.graph.sources import iter_source_files
    from oxn.languages import get_parser
    from oxn.profiles import profile_for_path
    from oxn.resolve.scopes import build_scopes
    from oxn.resolve.symbols import build_project_symbols
    from oxn.scip.index import load_index
    from oxn.scip.join import join_document

    index = load_index(index_path).by_path()
    # The grader measures the code the gate measures, `oxn.yaml`'s exclusions included:
    # resolution accuracy over vendored code is not a number about this project.
    from oxn.config import Config

    files = list(iter_source_files([root], base=root, exclude=Config.load(root).exclude))
    graph = build_dependency_graph(root, files)

    parsed_files: dict[str, tuple[LanguageProfile, Node, list[Entity]]] = {}
    entities_by_file: dict[str, list[Entity]] = {}
    scopes_by_file = {}

    for path in files:
        profile = profile_for_path(str(path))
        if profile is None:
            continue
        relative = path.resolve().relative_to(root).as_posix()
        source = path.read_bytes()
        tree = get_parser(profile.name).parse(source)
        if tree.root_node.has_error:
            continue
        parsed = build_file(relative, source, profile, tree.root_node)
        entities = list(parsed.entities)
        parsed_files[relative] = (profile, tree.root_node, entities)
        entities_by_file[relative] = entities
        scopes_by_file[relative] = build_scopes(tree.root_node, profile)

    symbols = build_project_symbols(entities_by_file, scopes_by_file, graph)

    # L2 ground truth: symbol -> entity id, across the whole tree.
    scip_to_entity: dict[str, str] = {}
    for relative, (profile, tree_root, entities) in parsed_files.items():
        document = index.get(relative)
        if document is None:
            continue
        result = join_document(entities, profile, tree_root, document)
        scip_to_entity.update(result.definitions)

    accuracy = ResolutionAccuracy(language=language)
    for relative, (profile, tree_root, _entities) in parsed_files.items():
        document = index.get(relative)
        if document is None:
            continue
        grade_file(
            _Grading(relative, profile, document, symbols, scip_to_entity, accuracy),
            tree_root,
        )
    return accuracy
