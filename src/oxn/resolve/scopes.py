"""L0: a lexical scope resolver over the CST.

Answers "what does this name refer to, here?" using nothing but syntax. That is enough for
locals, parameters, and file-level names -- 27% of all symbol occurrences in httpx are local
symbols, measured -- and it is *not* enough for anything needing an inferred type, which is
exactly what the L0-vs-L2 measurement quantifies rather than hand-waves.

Design follows scope graphs (Néron, Tolmach, Visser & Wachsmuth, ESOP 2015): a tree of
scopes, each holding bindings, with resolution walking outward. Building is file-at-a-time
so it stays incremental, which is what a cold-process hook needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from oxn.profiles.base import decorator_texts, receiver_type
from oxn.resolve.importers import IMPORT_READERS

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator

    from tree_sitter import Node

    from oxn.profiles.base import LanguageProfile
    from oxn.profiles.spec import ScopeSpec


@dataclass(frozen=True, slots=True)
class Binding:
    """A name introduced somewhere."""

    name: str
    kind: str
    line: int
    start_byte: int
    end_byte: int

    def __str__(self) -> str:
        return f"{self.name} ({self.kind}, line {self.line})"


@dataclass
class Scope:
    """One lexical region and the names it introduces."""

    kind: str
    start_byte: int
    end_byte: int
    parent: Scope | None = None
    bindings: dict[str, Binding] = field(default_factory=dict)
    children: list[Scope] = field(default_factory=list)
    #: The receiver name for a method -- read from the actual first parameter, never
    #: assumed to be ``self``, which is the crux of correct LCOM (docs/metrics.md 6.1).
    receiver: str | None = None
    #: Name of the enclosing definition, for diagnostics.
    owner: str | None = None
    #: Names this scope has handed to an outer one via ``global`` / ``nonlocal``. Assigning
    #: to such a name must not create a local, or the outer binding becomes unreachable.
    rebound: set[str] = field(default_factory=set)

    def __repr__(self) -> str:
        """Deliberately shallow: the default recursive repr prints the whole file's tree."""
        return (
            f"Scope({self.kind}, owner={self.owner!r}, "
            f"binds={sorted(self.bindings)}, children={len(self.children)})"
        )

    def declare(self, binding: Binding) -> None:
        if binding.name in self.rebound:
            return  # `global x` means x lives elsewhere, however it is assigned here
        # First declaration wins, so a function's parameters are not overwritten by a
        # later assignment to the same name.
        self.bindings.setdefault(binding.name, binding)

    def lookup(self, name: str) -> tuple[Binding, Scope] | None:
        """Resolve outward through enclosing scopes. Shadowing falls out of the order."""
        scope: Scope | None = self
        while scope is not None:
            found = scope.bindings.get(name)
            if found is not None:
                return found, scope
            # A class body is not in the lexical chain of the functions inside it: a method
            # cannot see its class's attributes as bare names. Skipping it here is what
            # makes `x` inside a method resolve to a global rather than to a class attribute.
            scope = scope.parent
            while scope is not None and scope.kind == "class":
                scope = scope.parent
        return None

    def contains(self, byte_offset: int) -> bool:
        return self.start_byte <= byte_offset < self.end_byte


@dataclass
class ScopeTree:
    """Every scope in one file."""

    root: Scope
    #: Class name -> the members declared on it, for cohesion metrics.
    class_members: dict[str, dict[str, Binding]] = field(default_factory=dict)
    #: Local alias -> the name it was imported as, e.g. ``np`` -> ``numpy``.
    import_aliases: dict[str, str] = field(default_factory=dict)
    #: Every name this file assigns to, anywhere.
    #:
    #: `Scope.declare` keeps the *first* binding for a name, so `from m import f` on line 1
    #: hides `f = wrap(f)` on line 3 and a lookup answers "import" for both. Any rule that
    #: must know a name was *re-bound* -- `scip.aliases`, which will not resolve a call
    #: through an import the file has since overwritten -- cannot get that from the scopes,
    #: so it is recorded here rather than each caller re-walking the tree to find out.
    #:
    #: **File-wide, not per scope.** A name assigned inside any function counts, so
    #: `def use(): f = 1; return f()` refuses a resolution the module-level import would
    #: have supported. That is the conservative direction, and the cost of the other one is
    #: a confidently wrong edge.
    assigned: set[str] = field(default_factory=set)

    def scope_at(self, byte_offset: int) -> Scope:
        """Innermost scope containing a byte offset."""
        scope = self.root
        while True:
            for child in scope.children:
                if child.contains(byte_offset):
                    scope = child
                    break
            else:
                return scope

    def resolve(self, name: str, byte_offset: int) -> tuple[Binding, Scope] | None:
        """What ``name`` refers to at a position."""
        return self.scope_at(byte_offset).lookup(name)

    def all_scopes(self) -> list[Scope]:
        out: list[Scope] = []
        stack = [self.root]
        while stack:
            scope = stack.pop()
            out.append(scope)
            stack.extend(scope.children)
        return out


def build_scopes(root: Node, profile: LanguageProfile) -> ScopeTree:
    """Build the scope tree for one file."""
    spec = profile.metrics.scopes
    module = Scope(kind="module", start_byte=root.start_byte, end_byte=root.end_byte)
    tree = ScopeTree(root=module)
    _walk(_Context(tree, profile, spec), root, module, class_name=None)
    return tree


@dataclass(frozen=True, slots=True)
class _Context:
    """What stays the same for one file's whole scope walk.

    The tree being filled in, and the two tables that say what the grammar means. Threading
    these as three separate arguments through a mutually recursive walk cost seven
    parameters and said nothing; only `node`, `scope` and `class_name` actually vary.
    """

    tree: ScopeTree
    profile: LanguageProfile
    spec: ScopeSpec


def _walk(context: _Context, node: Node, scope: Scope, *, class_name: str | None) -> None:
    """Every child: declare what it binds, then either open a scope or look inside it."""
    for child in node.named_children:
        target = context.profile.unwrap(child)
        kind = target.type

        # `global x` / `nonlocal x`: the name belongs to an outer scope, so recording it
        # here would create a phantom local that shadows the real binding.
        if kind in context.spec.rebinding_kinds:
            continue

        _declare(context, target, kind, scope)

        scope_kind = context.spec.scope_kinds.get(kind)
        if scope_kind is not None and scope_kind != "module":
            _enter(context, target, scope, scope_kind, class_name)
            continue

        _bind_from(target, scope, context.tree, context.profile, context.spec)
        _walk(context, target, scope, class_name=class_name)


def _declare(context: _Context, target: Node, kind: str, scope: Scope) -> None:
    """Record the name a definition or declaration introduces in the current scope."""
    if kind not in context.spec.declaration_kinds and not context.profile.is_definition(target):
        return
    name = context.profile.entity_name(target)
    if not name:
        return
    scope.declare(
        Binding(
            name,
            "class" if kind in context.profile.class_like else "function",
            target.start_point[0] + 1,
            target.start_byte,
            target.end_byte,
        )
    )


def _enter(
    context: _Context, target: Node, scope: Scope, scope_kind: str, class_name: str | None
) -> None:
    """Open a nested scope, populate it, and walk what it contains."""
    inner = Scope(
        kind=scope_kind,
        start_byte=target.start_byte,
        end_byte=target.end_byte,
        parent=scope,
        owner=context.profile.entity_name(target),
    )
    scope.children.append(inner)

    # `global`/`nonlocal` apply to the whole scope regardless of where they appear in it,
    # so they must be collected before any binding is recorded.
    inner.rebound = _rebound_names(target, context.profile, context.spec)
    if scope_kind in {"function", "lambda"}:
        inner.receiver = _bind_parameters(target, inner, context.profile, context.spec, class_name)

    # A nameless class scope inherits rather than erases. TypeScript opens one for the
    # `class_body` inside `class_declaration`, so reading `inner.owner` unconditionally
    # blanked the class name for every method in the file and left them all receiverless.
    next_class = (inner.owner or class_name) if scope_kind == "class" else class_name
    if scope_kind == "class" and inner.owner:
        context.tree.class_members.setdefault(inner.owner, {})

    _walk(context, target, inner, class_name=next_class)
    _collect_class_members(inner, context.tree, class_name=next_class)


def _rebound_names(node: Node, profile: LanguageProfile, spec: ScopeSpec) -> set[str]:
    """Names a scope declares ``global`` or ``nonlocal``, anywhere within it.

    The search stops at nested definitions: their declarations belong to them.
    """
    if not spec.rebinding_kinds:
        return set()

    names: set[str] = set()
    stack = list(node.named_children)
    while stack:
        current = stack.pop()
        if profile.is_definition(current) and current is not node:
            continue
        if current.type in spec.rebinding_kinds:
            names.update(
                _text(child)
                for child in current.named_children
                if child.type == spec.identifier_kind and _text(child)
            )
            continue
        stack.extend(current.named_children)
    return names


def _bind_parameters(
    node: Node, scope: Scope, profile: LanguageProfile, spec: ScopeSpec, class_name: str | None
) -> str | None:
    """Declare a callable's parameters; return its receiver name if it is a method.

    Two questions: which names this callable binds, and what its receiver is called.
    `_receiver_name` answers the second, and the answer feeds back here -- parameter zero is
    labelled a receiver only when it *is* the one, which is what stops Java's
    `save(String k)` from making `k` one.
    """
    receiver = _receiver_name(node, profile, spec, class_name)
    names = profile.parameter_names(node)
    for index, name in enumerate(names):
        if not name:
            continue
        scope.declare(
            Binding(
                name,
                "receiver" if (index == 0 and name == receiver) else "parameter",
                node.start_point[0] + 1,
                node.start_byte,
                node.end_byte,
            )
        )
    if receiver and receiver not in names:
        # Go writes its receiver outside the parameter list and Rust writes `&self`, so
        # neither reaches `parameter_names`. Unbound, `s` in `s.db.Write(k)` reads as a
        # free name rather than as this method's receiver.
        scope.declare(
            Binding(receiver, "receiver", node.start_point[0] + 1, node.start_byte, node.end_byte)
        )
    return receiver


def _receiver_name(
    node: Node, profile: LanguageProfile, spec: ScopeSpec, class_name: str | None
) -> str | None:
    """What this callable's receiver is called, or `None` when it has none.

    Four languages, four mechanisms, and the reason this dispatches on mechanism rather than
    on "is it inside a class" is that two of them are not:

    * **A declared receiver** (Go): `func (s *Service) Save(...)` is a *top-level*
      declaration, so there is no enclosing class scope to ask. The receiver field's presence
      is the whole test, and the name comes from it.
    * **A receiver among the parameters** (Rust): `fn save(&self)` is a method and
      `fn new(cfg: Config)` is an associated function, sitting side by side in one `impl`.
      The `self_parameter` node tells them apart; nothing else does, and treating parameter
      zero as a receiver would have made `cfg` one.
    * **An implicit receiver** (Java, TypeScript, JavaScript): the language passes none, so
      the spec names it, and it applies to anything inside a class.
    * **Parameter zero** (Python): read from the source, because assuming ``self`` breaks
      every codebase spelling it otherwise -- less ``@staticmethod``, which is in a class and
      has no receiver at all. Without that exclusion `parse(raw)` made `raw` the receiver and
      every `raw.strip()` became a field access on the class.
    """
    declared = receiver_type(node, profile.receiver_field)
    if declared is not None:
        return _declared_receiver_name(node, profile.receiver_field)
    if spec.self_parameter_kind:
        return "self" if _has_self_parameter(node, profile, spec) else None
    if not class_name or _is_static(node, profile, spec):
        return None
    if spec.implicit_receiver:
        return spec.implicit_receiver
    names = profile.parameter_names(node)
    return names[0] if names and spec.style == "python" else None


def _declared_receiver_name(node: Node, field: str) -> str | None:
    """The receiver's *variable* name in `func (s *Service) Save(...)` -- the `s`.

    `receiver_type` answers the other half, the `Service`. A receiver may be written with no
    name at all (`func (*Service) Save()`), and then there is nothing for an access to be
    attributed to, so it reports none rather than inventing one.
    """
    receiver = node.child_by_field_name(field)
    written = receiver.text.decode("utf-8", "replace") if receiver and receiver.text else ""
    parts = written.strip("()").strip().split()
    return parts[0] if len(parts) > 1 else None


def _has_self_parameter(node: Node, profile: LanguageProfile, spec: ScopeSpec) -> bool:
    """True when the parameter list carries the language's own receiver node."""
    parameters = node.child_by_field_name(profile.params_field)
    if parameters is None:
        return False
    return any(child.type == spec.self_parameter_kind for child in parameters.named_children)


def _is_static(node: Node, profile: LanguageProfile, spec: ScopeSpec) -> bool:
    """True when a decorator says this callable takes no receiver."""
    if not spec.static_markers:
        return False
    written = decorator_texts(node, profile.wrappers)
    return any(marker in text for text in written for marker in spec.static_markers)


def _bind_from(
    node: Node, scope: Scope, tree: ScopeTree, profile: LanguageProfile, spec: ScopeSpec
) -> None:
    """Record the names a statement introduces.

    Three unrelated statement shapes bind names, and they share nothing but the scope they
    bind into -- an assignment names its targets, an `as` clause names one alias, and an
    import names whatever it brings in and remembers where it came from.
    """
    kind = node.type
    if kind in spec.assignment_kinds:
        _bind_assignment(node, scope, tree, spec)
    elif kind in spec.alias_kinds:
        _bind_alias(node, scope)
    elif kind in profile.metrics.imports.statement_kinds:
        _bind_import(node, scope, tree, profile)


def _bind_assignment(node: Node, scope: Scope, tree: ScopeTree, spec: ScopeSpec) -> None:
    """Every identifier an assignment or loop binds, destructuring included."""
    for name_node in _binding_targets(node, spec):
        text = _text(name_node)
        if text:
            tree.assigned.add(text)
            scope.declare(
                Binding(
                    text,
                    "local",
                    name_node.start_point[0] + 1,
                    name_node.start_byte,
                    name_node.end_byte,
                )
            )


def _bind_alias(node: Node, scope: Scope) -> None:
    """`with x as y`, `except E as e`: the alias is the name that enters the scope."""
    alias = node.child_by_field_name("alias") or node.child_by_field_name("name")
    if alias is None or not _text(alias):
        return
    scope.declare(
        Binding(_text(alias), "alias", alias.start_point[0] + 1, alias.start_byte, alias.end_byte)
    )


def _bind_import(node: Node, scope: Scope, tree: ScopeTree, profile: LanguageProfile) -> None:
    """Imported names, plus the alias table that maps `np` back to `numpy`."""
    for name, source in _imported_names(node, profile):
        scope.declare(
            Binding(name, "import", node.start_point[0] + 1, node.start_byte, node.end_byte)
        )
        tree.import_aliases[name] = source


def _binding_targets(node: Node, spec: ScopeSpec) -> Iterator[Node]:
    """Identifiers bound by an assignment or loop, including destructured ones."""
    left = (
        node.child_by_field_name("left")
        or node.child_by_field_name("name")
        or node.child_by_field_name("pattern")
    )
    if left is None:
        return
    if left.type == spec.identifier_kind:
        yield left
        return
    stack = [left]
    while stack:
        current = stack.pop()
        if current.type == spec.identifier_kind:
            yield current
        elif current.type != spec.attribute_kind:
            # Do not descend into `a.b = 1`: that binds an attribute, not a local name.
            stack.extend(current.named_children)


def _imported_names(node: Node, profile: LanguageProfile) -> Iterator[tuple[str, str]]:
    """``(name bound locally, source specifier)`` for each name an import introduces.

    The *local* name is what matters for scoping and it is not always the source name:
    ``import numpy as np`` puts ``np`` in scope and ``numpy`` nowhere, and
    ``from a.b import c as d`` binds ``d``. Yielding the source name instead leaves every
    aliased import unresolvable.
    """
    spec = profile.metrics.imports
    reader = IMPORT_READERS.get(spec.style)
    if reader is not None:
        yield from reader(node)
        return
    if spec.style != "python":
        yield from _ecmascript_imported_names(node, profile)
        return
    if node.type == "import_statement":
        yield from _plain_import_names(node, profile)
        return
    yield from _from_import_names(node, profile)


def _ecmascript_imported_names(node: Node, profile: LanguageProfile) -> Iterator[tuple[str, str]]:
    """``(name, source)`` for each name an ECMAScript import binds.

    ``import a from "./m"`` binds ``a``, which the clause holds rather than the specifier;
    the specifier is the source, quoted in the syntax and stripped here.
    """
    spec = profile.metrics.imports
    source = node.child_by_field_name(spec.module_field)
    specifier = _strip_quotes(_text(source)) if source is not None else ""
    for clause in node.named_children:
        for name in _ecmascript_clause_names(clause):
            yield name, specifier


def _plain_import_names(node: Node, profile: LanguageProfile) -> Iterator[tuple[str, str]]:
    """``(name, source)`` for each name a plain ``import`` statement binds.

    ``import a.b`` binds the head ``a`` and points it at ``a.b``; ``import a.b as c``
    binds ``c`` and points it at the unaliased ``a.b``.
    """
    spec = profile.metrics.imports
    for child in node.children_by_field_name(spec.name_field):
        if child.type in spec.alias_kinds:
            alias = child.child_by_field_name("alias")
            target = child.child_by_field_name("name")
            if alias is not None and target is not None:
                yield _text(alias), _text(target)
        else:
            dotted = _text(child)
            head = dotted.split(".")[0]
            if head:
                yield head, dotted


def _from_import_names(node: Node, profile: LanguageProfile) -> Iterator[tuple[str, str]]:
    """``(name, source)`` for each name a ``from`` import binds.

    Every name points at the module specifier, aliased or not. ``from m import *`` binds
    no name that syntax alone can identify, so it contributes nothing.
    """
    spec = profile.metrics.imports
    module = node.child_by_field_name(spec.module_field)
    specifier = _text(module) if module is not None else ""
    for child in node.children_by_field_name(spec.name_field):
        if child.type in spec.alias_kinds:
            alias = child.child_by_field_name("alias")
            if alias is not None:
                yield _text(alias), specifier
        elif child.type not in spec.wildcard_kinds:
            name = _text(child)
            if name:
                yield name, specifier


def _ecmascript_clause_names(clause: Node) -> Iterator[str]:
    """Names bound by an ECMAScript import clause."""
    if clause.type not in {"import_clause", "namespace_import", "named_imports"}:
        return
    stack = [clause]
    while stack:
        current = stack.pop()
        if current.type == "import_specifier":
            alias = current.child_by_field_name("alias") or current.child_by_field_name("name")
            if alias is not None:
                yield _text(alias)
            continue
        if current.type == "identifier":
            yield _text(current)
            continue
        stack.extend(current.named_children)


def _strip_quotes(raw: str) -> str:
    return raw.strip().strip("\"'`")


def _collect_class_members(scope: Scope, tree: ScopeTree, *, class_name: str | None) -> None:
    """Record a class's methods and fields for the cohesion metrics of a later phase."""
    if scope.kind != "class" or not scope.owner:
        return
    members = tree.class_members.setdefault(scope.owner, {})
    members.update(scope.bindings)
    for child in scope.children:
        if child.kind in {"function", "lambda"} and child.owner:
            members.setdefault(
                child.owner,
                Binding(child.owner, "method", 0, child.start_byte, child.end_byte),
            )
    del class_name


def _text(node: Node | None) -> str:
    """The source text of a node, and ``""`` for a field that is not present.

    Widened to accept ``None`` when the Go/Rust/Java import readers arrived: every one of
    them reads optional grammar fields (`alias`, `scope`, `path`), and a `None` check at
    each call site is noise around the one thing they all mean by it -- absent."""
    if node is None:
        return ""
    return node.text.decode("utf-8", "replace") if node.text else ""
