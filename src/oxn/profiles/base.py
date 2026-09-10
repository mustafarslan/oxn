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
    #: Fields naming this type's supertypes. Every grammar spells it differently and the
    #: names are not interchangeable: Python has `superclasses`, Java `superclass` *and*
    #: `interfaces`, Rust the `trait` an `impl` block satisfies, and TypeScript none at all
    #: -- it hangs a `class_heritage` child off the declaration instead. Empty for Go, whose
    #: interfaces are satisfied structurally and never declared.
    #:
    #: `type_parameters` is deliberately absent. It was read as a supertype field, so
    #: `class Foo[T](Base)` reported bases `("Base", "T")` and counted a generic parameter
    #: as an ancestor in DIT and as a coupled class in CBO.
    supertype_fields: frozenset[str] = frozenset()
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
    #: * ``"hash"`` -- JavaScript: a ``#`` prefix, which is privacy the grammar guarantees.
    #: * ``""`` -- no name rule. See :attr:`privacy_rules` for the node-level ones.
    privacy: str = ""

    #: Every way this language can say "private", any of which is sufficient. Several apply
    #: at once: a TypeScript method may carry `private` *and* sit in a module that does not
    #: export it, and both are sound.
    #:
    #: This exists because `privacy` alone -- a question about a *name* -- left the
    #: `shredding` rule firing in **two of six launch languages**. Measured 2026-09-10 on one
    #: 14-helper shred transliterated across all six: Python and Go were caught by the rule,
    #: Java/TypeScript/JavaScript only because the fixture put the helpers in a class and the
    #: class aggregates caught *that*, and **Rust by nothing at all**. Moved module-level and
    #: written as free functions, TypeScript and JavaScript escaped too. Three languages with
    #: a free shred, while `oxn init` writes into every user's CLAUDE.md that shredding "is
    #: detected and rejected" -- which is P10's own finding, recurring one language at a time.
    #:
    #: * ``"name"`` -- ask :attr:`privacy` about the declared name.
    #: * ``"modifier"`` -- a child of :attr:`private_marker` holding the token ``private``.
    #: * ``"visibility"`` -- private *unless* a :attr:`public_marker` child is present, which
    #:   is Rust: no ``pub`` means private to the module.
    #: * ``"unexported"`` -- a top-level declaration not wrapped in :attr:`export_wrapper`,
    #:   and **only in a file that uses ES module syntax**. A CommonJS file can publish
    #:   anything through ``module.exports.x = x``, so the rule declines there rather than
    #:   guessing -- which is visible in the JavaScript corpus, whose imports are all
    #:   ``require``.
    privacy_rules: tuple[str, ...] = ()

    #: The child kind that holds a ``private`` token: Java's ``modifiers``, TypeScript's
    #: ``accessibility_modifier``.
    private_marker: str = ""

    #: The child kind whose *presence* makes a definition public: Rust's
    #: ``visibility_modifier``.
    public_marker: str = ""

    #: The wrapper that publishes a top-level declaration: ``export_statement``.
    export_wrapper: str = ""

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
        if self.privacy == "hash":
            return name.startswith("#")
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
            names.extend(_declared_names(child))
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


def declared_private(node: Node, name: str, profile: LanguageProfile, *, esm: bool) -> bool | None:
    """Is this definition private to its file? ``None`` when the profile cannot tell.

    ``None`` is not ``False`` and the distinction is the whole point: "called once in this
    file" does not prove a *public* name has no callers elsewhere, so a rule that needs
    privacy must decline rather than guess.

    Module-level beside `receiver_type` and `implemented_type`, and for the same reason those
    are -- `LanguageProfile` sits at its `weighted_methods_per_class` ceiling, and this is a
    question about a node rather than about the profile.

    Any rule the profile declares may answer yes. The rules are checked in the order declared
    so the cheapest and most local one wins: a Java method carrying `private` is private
    whatever its file does.
    """
    for rule in profile.privacy_rules:
        found = _by_rule(rule, node, name, profile, esm=esm)
        if found:
            return True
    return False if profile.privacy_rules else None


def _by_rule(rule: str, node: Node, name: str, profile: LanguageProfile, *, esm: bool) -> bool:
    """One privacy rule, applied. Unknown rules answer no rather than raising."""
    if rule == "name":
        return bool(profile.is_private(name))
    if rule == "modifier":
        return _has_token(node, profile.private_marker, "private")
    if rule == "visibility":
        # Any `visibility_modifier` counts as public, `pub(crate)` and `pub(super)` included.
        # That is deliberate and conservative: both are reachable from other files in the
        # crate, so "called once in this file" does not show a helper is dedicated. Reading
        # them as private would make the rule fire on code with callers it cannot see.
        return not any(child.type == profile.public_marker for child in node.named_children)
    if rule == "unexported":
        wrapper = profile.export_wrapper
        return esm and _is_top_level(node, wrapper) and not _is_wrapped_in(node, wrapper)
    return False


def _has_token(node: Node, holder: str, token: str) -> bool:
    """Does a child of `holder` kind carry this exact token? Java and TypeScript both do."""
    for child in node.named_children:
        if child.type != holder:
            continue
        if any(grandchild.type == token for grandchild in child.children):
            return True
    return False


def _is_top_level(node: Node, wrapper: str) -> bool:
    """Is this declaration at the top of its file, rather than nested inside something?

    Only a top-level declaration is governed by whether the *module* exports it. One inside a
    class or a function has its own scope and its own answer.

    The wrapper is the caller's, not a set written in here: a second table beside a
    configurable one is what `oxn.languages.EXTENSIONS` was, and it disagreed with the first.
    """
    parent = node.parent
    if parent is not None and wrapper and parent.type == wrapper:
        parent = parent.parent
    return parent is not None and parent.parent is None


def _is_wrapped_in(node: Node, wrapper: str) -> bool:
    return bool(wrapper) and node.parent is not None and node.parent.type == wrapper


def implemented_type(node: Node, field: str) -> str:
    """The bare name of the type a member-holding block belongs to -- Rust's `impl Service`.

    Module-level beside `receiver_type`, for the same reason and because both the member
    model and the package aggregate must answer it identically: a type whose methods are
    counted one way by `oxn classes` and another by the gate has two method counts, and the
    smaller one is an evasion.

    `impl Display for Service` names the type in this field and the trait in another, so both
    forms answer `Service`. Generic arguments are dropped, so `impl<'b, R> Reader<'b, R>` and
    `struct Reader` are one type -- the same normalisation `receiver_type` applies to Go.
    """
    named = node.child_by_field_name(field) if field else None
    written = named.text.decode("utf-8", "replace") if named is not None and named.text else ""
    return written.split("<")[0].strip()


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


#: What a name looks like once the grammar has been asked for one. A class member's name is
#: a `property_identifier` in both ECMAScript grammars, and requiring a bare `identifier` is
#: why `handler = (x) => x` stayed anonymous there while Java's `Runnable a = () -> {}` did
#: not -- one shape, two answers, and the two class ceilings inert on the ECMAScript side.
_NAME_KINDS = frozenset({"identifier", "property_identifier"})

#: `property` is here for JavaScript's `field_definition`, whose name sits under that field
#: where TypeScript's `public_field_definition` puts it under `name`. Without it the two
#: grammars disagreed about the same source: `handler = (x) => x` was a named member in
#: TypeScript and anonymous in JavaScript.
_BINDING_FIELDS = ("name", "left", "pattern", "property")

#: Grammar nodes that stand between a declaration and the value it binds while naming
#: nothing themselves. Go's `expression_list` holds the value of `g := func() {}` exactly as
#: it holds `var Fn = func() {}`'s, and a parenthesis wraps `f = (lambda: 1)` in every
#: language here. Named rather than "any parent holding one child", which is what this was:
#: a one-element container is not a wrapper, and `xs = [lambda: 1]` read through the list to
#: call the lambda `xs`. A parenthesis is safe to include only because `_USE_KINDS` below
#: stops `(function () {}).bind(this)` at the member expression.
_VALUE_WRAPPERS = frozenset({"expression_list", "parenthesized_expression"})

#: Parents that hold a name *field* while declaring nothing. A keyword argument names the
#: parameter it fills, so `sorted(items, key=lambda v: v)` bound its lambda the name `key` --
#: a name no call site can reach it by, in the table that exists to be reached by name. An
#: attribute access names a member of the value on its left, which is what `property` above
#: would otherwise pick up from `(function () {}).bind(this)`. One entry per grammar's
#: spelling of the two, because a set that covers four languages and misses two is the shape
#: of every cross-language defect in this file.
_USE_KINDS = frozenset(
    {
        "keyword_argument",
        "member_expression",
        "attribute",
        "field_expression",
        "selector_expression",
        "field_access",
    }
)


def _bound_name(node: Node) -> Node | None:
    """The identifier a callable was assigned to, when the callable has no name of its own.

    Only the immediate parent, and only when it yields a plain identifier: `const [a, b] =
    ...` destructures, `obj.method = () => {}` binds an attribute, and neither is a name this
    callable can be called by from the bare-name table this feeds.
    """
    parent = node.parent
    if parent is not None and parent.type in _VALUE_WRAPPERS and parent.named_children == [node]:
        # A wrapper carrying no name of its own: Go puts the value of `var Fn = func() {}`
        # inside an `expression_list`. Only when it is the *sole* child, so that
        # `a, b = 1, func() {}` -- where names bind positionally and `a` is not this
        # function's name -- stays correctly anonymous. Named rather than "any parent holding
        # one child", which is what it was: a one-element container is not a wrapper, and
        # `xs = [lambda: 1]` was reading through the list to call the lambda `xs`.
        parent = parent.parent
    if parent is None or parent.type in _USE_KINDS:
        return None
    for binding_field in _BINDING_FIELDS:
        bound = _sole_identifier(parent.child_by_field_name(binding_field))
        if bound is not None and bound != node:
            return bound
    return None


def _sole_identifier(bound: Node | None) -> Node | None:
    """A name, seen through a wrapper holding exactly one.

    Go wraps the *name* of `g := func() {}` in an `expression_list` exactly as it wraps the
    value, so the `var Fn = func() {}` form took its name and the short form did not -- one
    language disagreeing with itself. Only when it is the sole child, so that
    `a, b := 1, func() {}`, where names bind positionally and `a` is not this function's
    name, stays correctly anonymous.
    """
    if bound is None:
        return None
    if bound.type not in _NAME_KINDS and len(bound.named_children) == 1:
        bound = bound.named_children[0]
    return bound if bound.type in _NAME_KINDS else None


def _declared_names(node: Node) -> list[str]:
    """Every parameter one node declares, which is not always one.

    Go groups parameters that share a type: `func f(a, b, c int, d string)` is **two**
    `parameter_declaration` nodes, the first holding three `name` fields. Reading the first
    identifier of each node counted that signature as two parameters, and five as one -- so
    `parameter_count` was gated on a number no other language computed the same way, and Go's
    0.28% exceedance was measuring the grammar rather than the code.

    Only when the grammar really hands back several, so every other language keeps the
    `_first_identifier` answer: a Python `identifier`, a TypeScript `required_parameter`
    (named under `pattern`) and a Rust `parameter` all yield exactly one.
    """
    named = node.children_by_field_name("name")
    if len(named) > 1:
        return [child.text.decode("utf-8", "replace") for child in named if child.text]
    return [_first_identifier(node)]


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
