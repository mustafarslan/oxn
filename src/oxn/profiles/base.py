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

from collections.abc import Mapping
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
    comment_kinds: frozenset[str] = frozenset()
    string_kinds: frozenset[str] = frozenset()
    #: Modifier tokens that mark a type as abstract at the syntax level.
    abstract_markers: frozenset[str] = frozenset()
    #: Node kinds that are abstract types purely by virtue of their kind.
    abstract_kinds: frozenset[str] = frozenset()

    #: Metric tables: decision points, cognitive increment classes, statement kinds.
    metrics: MetricSpec = field(default_factory=MetricSpec)

    #: Reserved. Tree-sitter S-expression queries by purpose, e.g. ``{"imports": "..."}``.
    #: Empty so far; see the module docstring.
    queries: Mapping[str, str] = field(default_factory=dict)

    # ---- helpers -------------------------------------------------------------------

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
        """The declared name of a definition node, or ``None`` for anonymous callables."""
        name_node = node.child_by_field_name(self.name_field)
        if name_node is None:
            return None
        return name_node.text.decode("utf-8", "replace") if name_node.text else None

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
