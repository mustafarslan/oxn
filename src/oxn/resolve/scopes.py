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
    _walk(root, module, tree, profile, spec, class_name=None)
    return tree


def _walk(
    node: Node,
    scope: Scope,
    tree: ScopeTree,
    profile: LanguageProfile,
    spec: ScopeSpec,
    *,
    class_name: str | None,
) -> None:
    for child in node.named_children:
        target = profile.unwrap(child)
        kind = target.type

        if kind in spec.rebinding_kinds:
            # `global x` / `nonlocal x`: the name belongs to an outer scope, so recording it
            # here would create a phantom local that shadows the real binding.
            continue

        if kind in spec.declaration_kinds or profile.is_definition(target):
            name = profile.entity_name(target)
            if name:
                scope.declare(
                    Binding(
                        name,
                        "class" if kind in profile.class_like else "function",
                        target.start_point[0] + 1,
                        target.start_byte,
                        target.end_byte,
                    )
                )

        scope_kind = spec.scope_kinds.get(kind)
        if scope_kind is not None and scope_kind != "module":
            inner = Scope(
                kind=scope_kind,
                start_byte=target.start_byte,
                end_byte=target.end_byte,
                parent=scope,
                owner=profile.entity_name(target),
            )
            scope.children.append(inner)

            # `global`/`nonlocal` apply to the whole scope regardless of where they appear
            # in it, so they must be collected before any binding is recorded.
            inner.rebound = _rebound_names(target, profile, spec)
            if scope_kind in {"function", "lambda"}:
                inner.receiver = _bind_parameters(target, inner, profile, spec, class_name)
            next_class = inner.owner if scope_kind == "class" else class_name
            if scope_kind == "class" and inner.owner:
                tree.class_members.setdefault(inner.owner, {})

            _walk(target, inner, tree, profile, spec, class_name=next_class)
            _collect_class_members(inner, tree, class_name=next_class)
            continue

        _bind_from(target, scope, tree, profile, spec)
        _walk(target, scope, tree, profile, spec, class_name=class_name)


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

    The receiver is the *actual* first parameter, read from the source. Assuming ``self``
    silently breaks every codebase that spells it differently, and Python has no syntactic
    receiver at all.
    """
    names = profile.parameter_names(node)
    for index, name in enumerate(names):
        if not name:
            continue
        scope.declare(
            Binding(
                name,
                "receiver" if (index == 0 and class_name) else "parameter",
                node.start_point[0] + 1,
                node.start_byte,
                node.end_byte,
            )
        )
    if class_name and names and spec.style == "python":
        return names[0]
    if class_name and spec.style == "ecmascript":
        return "this"
    return None


def _bind_from(
    node: Node, scope: Scope, tree: ScopeTree, profile: LanguageProfile, spec: ScopeSpec
) -> None:
    """Record the names a statement introduces."""
    kind = node.type

    if kind in spec.assignment_kinds:
        for name_node in _binding_targets(node, spec):
            text = _text(name_node)
            if text:
                scope.declare(
                    Binding(
                        text,
                        "local",
                        name_node.start_point[0] + 1,
                        name_node.start_byte,
                        name_node.end_byte,
                    )
                )
        return

    if kind in spec.alias_kinds:
        alias = node.child_by_field_name("alias") or node.child_by_field_name("name")
        if alias is not None and _text(alias):
            scope.declare(
                Binding(
                    _text(alias),
                    "alias",
                    alias.start_point[0] + 1,
                    alias.start_byte,
                    alias.end_byte,
                )
            )
        return

    imports = profile.metrics.imports
    if kind in imports.statement_kinds:
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


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace") if node.text else ""
