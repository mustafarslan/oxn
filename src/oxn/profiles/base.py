"""The ``LanguageProfile`` contract.

A profile is the *only* place language-specific knowledge lives. Everything above it --
the graph builder, and later every metric -- reads the profile and never touches a grammar
directly. Adding a language means adding a profile and a golden corpus; it must never mean
editing a metric.

**Deviation from docs/metrics.md section 1.4, deliberate.** That spec proposed
``queries/*.scm`` files as the primary mechanism. Probing the real grammars showed the
discriminations that actually matter are *structural context*, which flat query captures
express badly:

* a Python ``if_statement`` carries **several** ``alternative`` fields -- ``elif_clause``
  and ``else_clause`` are siblings under the same field name;
* in TS an ``else if`` is an ``else_clause`` whose named child is an ``if_statement``
  rather than a ``statement_block``, so the test is "look inside", not "check the type";
* Python's ``decorated_definition`` and TS's ``export_statement`` *wrap* the definition
  that is the real entity.

So profiles are node-kind data consumed by visitors. :attr:`LanguageProfile.queries` is
reserved for the places where a query genuinely wins -- import extraction in P4 is the
likely first -- and can be filled without a schema break.

**Versioning.** :attr:`LanguageProfile.version` is part of the cache key. Bump it in the
same commit as any change to a profile's node-kind sets, or the cache will serve results
computed under the old tables.
"""

from __future__ import annotations

from collections.abc import Container, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from oxn.profiles.spec import MetricSpec

if TYPE_CHECKING:  # pragma: no cover
    from tree_sitter import Node


@dataclass(frozen=True)
class Wrapper:
    """A node that wraps the definition which is the real entity.

    Python's ``decorated_definition`` and TypeScript's ``export_statement`` both wrap a
    function or class. The entity is the inner definition; the wrapper contributes only its
    span (so a decorator's lines belong to the function) and its modifiers.
    """

    kind: str
    #: Field name holding the wrapped definition, or ``None`` to take the first named child.
    field_name: str | None = None


@dataclass(frozen=True)
class LanguageProfile:
    """Everything OXN knows about one language's syntax.

    The structural layer feeds the graph builder; :attr:`metrics` feeds the metric engine.
    Halstead classification tables and the semantic abstractness rules arrive later, as
    further fields with defaults.
    """

    name: str
    grammar: str
    extensions: frozenset[str]
    #: Bump on any change to the tables below. Part of the cache key.
    version: int

    # ---- structural: what the skeleton graph builder needs -------------------------
    #: Nodes that introduce a callable entity and a new nesting scope.
    function_like: frozenset[str]
    #: Nodes that introduce a type: class, interface, trait, struct, enum, type alias.
    class_like: frozenset[str]
    #: Nodes that wrap a definition (decorators, ``export``). Keyed by node kind.
    wrappers: Mapping[str, Wrapper]
    #: Field holding an entity's identifier.
    name_field: str = "name"
    #: Field holding an entity's body.
    body_field: str = "body"
    #: Field holding a callable's parameter list.
    params_field: str = "parameters"
    #: Node kinds inside a parameter list that count as one parameter each.
    parameter_kinds: frozenset[str] = frozenset()
    #: Parameter kinds that are a receiver (``self``, ``this``) rather than a parameter.
    receiver_kinds: frozenset[str] = frozenset()
    #: Field naming the *type a callable is a method of*, where the grammar puts it outside
    #: any class body. Go alone needs this: `func (c *Counter) Inc()` is a top-level
    #: declaration, so containment cannot say it belongs to `Counter` and the receiver must.
    receiver_field: str = ""
    #: Field naming the type a body implements, where the members live apart from the type:
    #: Rust's ``impl Service { ... }``. Such a node carries no name of its own, so without
    #: this it is skipped and the type keeps none of its methods.
    implements_field: str = ""
    #: Names that make a method's *first* parameter its receiver, where the grammar gives
    #: the receiver no node kind of its own. Rust has `self_parameter` and needs none of
    #: this; Python spells the receiver as an ordinary identifier, so without these a
    #: five-parameter method measured six and every Python method was charged for `self`.
    #: A ceiling has to mean the same thing for a method as for a function.
    receiver_names: frozenset[str] = frozenset()
    comment_kinds: frozenset[str] = frozenset()
    string_kinds: frozenset[str] = frozenset()
    #: Modifier tokens that mark a type as abstract at the syntax level.
    abstract_markers: frozenset[str] = frozenset()
    #: Node kinds that are abstract types purely by virtue of their kind.
    abstract_kinds: frozenset[str] = frozenset()

    #: How the language marks a name as not part of the public surface, by *name alone*.
    #:
    #: * ``"underscore"`` -- Python: a leading underscore.
    #: * ``"casing"`` -- Go: a lowercase initial letter.
    #: * ``""`` -- the profile cannot answer from the name. Java, Rust and TypeScript spell
    #:   visibility with modifiers (``private``, ``pub``, ``#``), which is a node-level
    #:   question this field deliberately does not pretend to answer. The shredding rule
    #:   needs privacy to be sound and therefore does not fire for such a language.
    privacy: str = ""

    #: Metric tables: decision points, cognitive increment classes, statement kinds.
    metrics: MetricSpec = field(default_factory=MetricSpec)

    #: Reserved. Tree-sitter S-expression queries by purpose, e.g. ``{"imports": "..."}``.
    #: Empty so far; see the module docstring.
    queries: Mapping[str, str] = field(default_factory=dict)

    # ---- helpers -------------------------------------------------------------------

    def is_private(self, name: str) -> bool | None:
        """Is this name private to its module? ``None`` when the profile cannot tell.

        ``None`` is not ``False``: "called once in this file" does not prove a *public*
        name has no callers elsewhere, so a rule that needs privacy must decline rather
        than guess. See :attr:`privacy`.
        """
        if not name:
            return None
        if self.privacy == "underscore":
            return name.startswith("_")
        if self.privacy == "casing":
            return not name[:1].isupper()
        return None

    def is_definition(self, node: Node) -> bool:
        """True if this node is itself an entity OXN records."""
        return node.type in self.function_like or node.type in self.class_like

    def unwrap(self, node: Node) -> Node:
        """Follow wrapper nodes down to the definition they wrap.

        Returns ``node`` unchanged when it is not a wrapper. Chained wrappers (an exported
        decorated class) are followed to the end.
        """
        seen = 0
        current = node
        while (wrapper := self.wrappers.get(current.type)) is not None and seen < 8:
            inner = (
                current.child_by_field_name(wrapper.field_name)
                if wrapper.field_name
                else next((c for c in current.named_children if self.is_definition(c)), None)
            )
            if inner is None:
                return current
            current, seen = inner, seen + 1
        return current

    def entity_name(self, node: Node) -> str | None:
        """The declared name of a definition node, or ``None`` for a truly anonymous one.

        **A callable bound to a name is not anonymous**, and treating it as one cost more
        call sites than any other single gap: `const handler = () => {}`,
        `var Fn = func() {}`, `f = lambda: 1` all produced an entity with `name=None`, which
        `_declared_in` skips -- so nothing could ever resolve a call to them. Measured across
        the pinned corpora, 575 call sites reach a callee SCIP places in the tree and OXN had
        no *named* entity for, on top of the closures below it.

        The binding field differs by grammar -- `name`, `left`, `pattern` -- which is the
        same set `_binding_targets` already walks for assignments, for the same reason.
        """
        name_node = self.name_node(node)
        if name_node is None:
            return None
        return name_node.text.decode("utf-8", "replace") if name_node.text else None

    def name_node(self, node: Node) -> Node | None:
        """The node that *names* a definition, which is not always inside it.

        A single answer for both readers, because they must agree: `entity_name` records
        what an entity is called, and `scip/join.py` matches an entity to a SCIP symbol by
        looking up the occurrence at this node's position. Naming a bound callable without
        teaching the join the same thing produced entities that could be *looked up* by name
        and never *graded*, which is a measurement that cannot see its own subject.
        """
        return node.child_by_field_name(self.name_field) or _bound_name(node)

    def parameter_names(self, node: Node) -> list[str]:
        """Declared parameter names, receivers excluded.

        Receiver identification reads the *actual* first parameter rather than assuming
        ``self`` -- docs/metrics.md section 6.1 makes this the crux of correct LCOM.
        """
        params = node.child_by_field_name(self.params_field)
        if params is None:
            return []
        names: list[str] = []
        for child in params.named_children:
            if child.type in self.receiver_kinds:
                continue
            if self.parameter_kinds and child.type not in self.parameter_kinds:
                continue
            names.append(_first_identifier(child))
        return names


#: Fields under which a declaration holds the name it binds. `name` covers a TypeScript
#: `variable_declarator` and a Go `var_spec`, `left` a Python assignment, `pattern` a Rust
#: `let`. The same three `_binding_targets` walks, and for the same reason.
def receiver_type(node: Node, field: str) -> str | None:
    """The bare name of the type a callable is a method of, or ``None``.

    Module-level rather than a `LanguageProfile` method for a reason the gate supplied: that
    class sits at its `weighted_methods_per_class` ceiling, and adding a sixth branch to it
    failed the check. `_bound_name` below is there for the same shape of reason.

    Pointer and value receivers name the same type -- `(c *Counter)` and `(c Counter)` are
    both methods of `Counter` -- so the receiver's own variable name, a leading `*` and any
    type parameters are all dropped.
    """
    receiver = node.child_by_field_name(field) if field else None
    written = receiver.text.decode("utf-8", "replace") if receiver and receiver.text else ""
    parts = written.strip("()").strip().split()
    return parts[-1].lstrip("*").split("[")[0] or None if parts else None


def decorator_texts(node: Node, wrappers: Container[str]) -> tuple[str, ...]:
    """The decorators attached to a definition, exactly as written.

    Decorators hang off the *wrapper* node rather than the definition itself, so the
    profile's `wrappers` set is the entry point rather than the node. Text is returned
    unparsed because callers match loosely: `@property`, `@x.setter` and
    `@functools.cached_property` are all the property question, and pretending otherwise
    would need a decorator resolver this does not have.

    Module-level for the same reason as `receiver_type`, and shared because both the scope
    binder and the member model ask this question.
    """
    parent = node.parent
    if parent is None or parent.type not in wrappers:
        return ()
    return tuple(
        child.text.decode("utf-8", "replace")
        for child in parent.named_children
        if child.type == "decorator" and child.text
    )


_BINDING_FIELDS = ("name", "left", "pattern")


def _bound_name(node: Node) -> Node | None:
    """The identifier a callable was assigned to, when the callable has no name of its own.

    Only the immediate parent, and only when it yields a plain identifier: `const [a, b] =
    ...` destructures, `obj.method = () => {}` binds an attribute, and neither is a name this
    callable can be called by from the bare-name table this feeds.
    """
    parent = node.parent
    if parent is not None and parent.named_children == [node]:
        # A wrapper carrying no name of its own: Go puts the value of `var Fn = func() {}`
        # inside an `expression_list`. Only when it is the *sole* child, so that
        # `a, b = 1, func() {}` -- where names bind positionally and `a` is not this
        # function's name -- stays correctly anonymous.
        parent = parent.parent
    if parent is None:
        return None
    for binding_field in _BINDING_FIELDS:
        bound = parent.child_by_field_name(binding_field)
        if bound is not None and bound.type == "identifier" and bound != node:
            return bound
    return None


def _first_identifier(node: Node) -> str:
    """Best-effort parameter name: the node's own text, or its first identifier child."""
    if node.type in {"identifier", "type_identifier"}:
        return node.text.decode("utf-8", "replace") if node.text else ""
    stack = list(node.named_children)
    while stack:
        child = stack.pop(0)
        if child.type == "identifier":
            return child.text.decode("utf-8", "replace") if child.text else ""
        stack.extend(child.named_children)
    return node.text.decode("utf-8", "replace") if node.text else ""
