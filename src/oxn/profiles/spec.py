"""Per-language metric tables.

These encode Appendix B of the Cognitive Complexity specification (G. Ann Campbell,
SonarSource, v1.7, 29 August 2023) and the decision-point table for McCabe's Cyclomatic
Complexity, as node kinds for one grammar.

The four increment classes come straight from the specification, and the distinction
between them is the part reimplementations get wrong:

============  =========  =============  ==================  ====================
class         B1: +1     B2: raises     B3: receives        example
                         nesting        nesting increment
============  =========  =============  ==================  ====================
structural    yes        yes            yes                 ``if``, ``for``, ``catch``
hybrid        yes        yes            **no**              ``else``, ``elif``
nesting_only  **no**     yes            no                  lambdas, nested functions
fundamental   yes        no             no                  labelled jumps, boolean sequences
============  =========  =============  ==================  ====================

``else`` and ``elif`` are hybrid because "the mental cost has already been paid when
reading the if". Lambdas are nesting-only: no increment of their own, but code inside them
is nested. Getting either wrong skews every score in the codebase.
"""

from __future__ import annotations

from collections.abc import Mapping  # noqa: TC003 - used in a runtime default
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CyclomaticSpec:
    """Decision points for McCabe complexity: ``M = pi + 1``.

    There is no cross-language standard, so **this table is the definition**. OXN's rule is
    "count what branches the control-flow graph", and each divergence from a free oracle is
    documented in ``docs/divergences.md``.
    """

    #: Node kinds that add one decision point each.
    decision_points: frozenset[str] = frozenset()
    #: Multiway constructs the language makes **exhaustive**, so there is no implicit
    #: "nothing matched" path. Such a construct with ``n`` branches has ``n`` paths and
    #: therefore ``n - 1`` decision points -- a two-arm Rust ``match`` is exactly an
    #: ``if``/``else`` and must score the same. A C-style ``switch`` is *not* exhaustive:
    #: its cases plus the implicit fall-through give ``n`` paths for ``n`` cases, which is
    #: why ``default`` is excluded there instead.
    exhaustive_multiway_kinds: frozenset[str] = frozenset()
    #: Branch kinds counted inside an exhaustive multiway construct.
    multiway_branch_kinds: frozenset[str] = frozenset()

    #: Decision kinds that must be skipped when they are a catch-all: a ``match`` arm of
    #: ``_``, a ``case _``. A default branch is the fall-through, not a branch of its own --
    #: the same rule that already excludes ``default`` from a ``switch``. Counting it makes
    #: a two-arm ``match`` score higher than the ``if``/``else`` it is equivalent to.
    catch_all_kinds: frozenset[str] = frozenset()
    #: Pattern kinds that make a branch a catch-all.
    catch_all_pattern_kinds: frozenset[str] = frozenset()

    #: Binary-operator node kind whose operator field is inspected (``&&``, ``and``, ...).
    boolean_node: str | None = None
    #: Operators inside :attr:`boolean_node` that count. Each occurrence adds one.
    boolean_operators: frozenset[str] = frozenset()


@dataclass(frozen=True)
class CognitiveSpec:
    """Cognitive Complexity increment classes for one grammar."""

    structural: frozenset[str] = frozenset()
    hybrid: frozenset[str] = frozenset()
    nesting_only: frozenset[str] = frozenset()
    fundamental: frozenset[str] = frozenset()
    #: Ignored entirely -- ``try`` and ``finally`` per the specification.
    ignored: frozenset[str] = frozenset()

    #: Jump kinds that increment **only when they carry a label or level**. The
    #: specification is precise: `goto LABEL`, `break LABEL`, `continue LABEL`,
    #: `break NUMBER`, `continue NUMBER`. A plain `break`, `continue` or early `return`
    #: never increments, "because an early return can often make code much clearer".
    labelled_jump_kinds: frozenset[str] = frozenset()
    #: Node kinds that appear as a jump's label child.
    jump_label_kinds: frozenset[str] = frozenset({"statement_identifier", "identifier", "number"})

    #: Binary-operator node kind for the boolean-sequence rule.
    boolean_node: str | None = None
    #: Operators that participate in a sequence.
    boolean_operators: frozenset[str] = frozenset()

    #: Kinds whose `for`/`if` clauses are siblings of the body rather than its ancestors,
    #: so nesting cannot be derived from the tree shape alone. Comprehensions, in practice.
    comprehension_kinds: frozenset[str] = frozenset()
    #: Clause kinds inside a comprehension that act as loops.
    comprehension_loop_kinds: frozenset[str] = frozenset()
    #: Clause kinds inside a comprehension that act as filters.
    comprehension_filter_kinds: frozenset[str] = frozenset()
    #: Field holding a comprehension's result expression.
    comprehension_body_field: str = "body"

    #: A hybrid clause counts only under these parents. A loop's ``else`` is not an
    #: ``if``/``else``, and counting it would diverge from every other implementation.
    hybrid_parents: frozenset[str] = frozenset({"if_statement"})

    #: Fields whose children stay at the *parent's* nesting level rather than one deeper.
    #: ``alternative`` holds ``elif``/``else``, which must not be nested under their ``if``.
    same_nesting_fields: frozenset[str] = frozenset({"alternative"})

    #: How the grammar spells ``else`` and ``else if``. Three shapes exist among the launch
    #: languages, verified against each grammar rather than assumed:
    #:
    #: * ``"clause"`` -- Python: dedicated ``elif_clause`` and ``else_clause`` nodes sit in
    #:   the ``alternative`` field, and there may be several of them.
    #: * ``"wrapped"`` -- TypeScript, JavaScript, Rust: ``alternative`` holds an
    #:   ``else_clause`` which *contains* the next ``if``. The pair is one hybrid increment.
    #: * ``"direct"`` -- Go, Java: ``alternative`` holds the next ``if`` node itself, or a
    #:   bare block for a plain ``else``. There is no ``else`` node to match on at all.
    alternative_style: str = "clause"
    #: Kinds appearing in ``alternative`` that mean a plain ``else`` under ``"direct"``.
    plain_else_kinds: frozenset[str] = frozenset({"block", "statement_block"})

    #: Legacy alias for ``alternative_style == "wrapped"``.
    else_if_via_else_clause: bool = False
    #: The wrapper kind for the rule above.
    else_clause_kind: str = "else_clause"
    #: The conditional kind the wrapper may contain.
    if_kind: str = "if_statement"

    #: Compensating usages from Appendix A that this language enables.
    python_decorator_exception: bool = False
    js_declarative_function_exception: bool = False

    #: Node kinds that call something, used for the direct-recursion increment.
    call_kinds: frozenset[str] = frozenset()
    #: Field of a call node holding the callee.
    callee_field: str = "function"


@dataclass(frozen=True)
class SizeSpec:
    """Node kinds needed for line and statement counting."""

    #: Statement-like kinds counted for logical lines of code.
    statement_kinds: frozenset[str] = frozenset()
    #: Kinds that end a function's execution, counted as exit points.
    return_kinds: frozenset[str] = frozenset()
    #: Node kinds whose direct children stand where a *statement* stands: Python's `block`
    #: and `module`, Go's `statement_list`, a JavaScript `statement_block`.
    #:
    #: `statement_kinds` alone cannot count logical lines, because a grammar need not wrap a
    #: statement in a node named for its being one. The Python grammar OXN ships emits
    #: `block > assignment` and `block > call` directly -- there is no `expression_statement`
    #: -- so the two commonest statements in the language counted zero, and `lloc` over
    #: OXN's own `src/` read **4,128 against radon's 9,582: 43%**. Two bare calls on two
    #: lines scored one logical line between them.
    #:
    #: Empty leaves the kind-based rule alone, so a profile that does not set this is
    #: unchanged.
    statement_containers: frozenset[str] = frozenset()

    #: True when a bare string expression statement is documentation rather than data.
    docstrings_are_comments: bool = False
    #: True where a function body's final *expression* is its return value, with no keyword
    #: -- Rust. Such a function leaves through that expression exactly as every other
    #: language leaves through a `return`, and not counting it made the same logic read four
    #: exits in Rust against five everywhere else.
    tail_expression_returns: bool = False


@dataclass(frozen=True)
class HalsteadSpec:
    """Operator/operand classification for Halstead's measures.

    **There is no canonical classification, in any language.** Halstead defined it for
    Fortran in 1977; radon, lizard and rust-code-analysis each disagree. So this table is
    OXN's published, versioned definition, and oracles are used for *correlation* only,
    never equality. See docs/metrics.md section 3.6.
    """

    #: Version of the classification policy. Changing the tables below must bump this.
    spec_version: int = 1
    #: Named leaf kinds counted as operands (identifiers, literals, type names).
    operand_kinds: frozenset[str] = frozenset()
    #: Opening delimiters. Halstead's convention: a matched pair is ONE operator.
    open_delimiters: frozenset[str] = frozenset({"(", "[", "{"})
    #: Closing delimiters, skipped so the pair is not double-counted.
    close_delimiters: frozenset[str] = frozenset({")", "]", "}"})
    #: Tokens excluded from both counts (layout, block structure).
    excluded_tokens: frozenset[str] = frozenset()
    #: Named kinds whose subtree is skipped entirely (comments, docstrings).
    excluded_kinds: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ImportSpec:
    """How one language spells an import.

    Two *styles* cover the launch set, because import syntax clusters into families rather
    than varying per language: ``python`` (dotted module paths, relative levels expressed as
    leading dots, names imported *from* a module) and ``ecmascript`` (a quoted specifier
    that is a path or a package). Adding Go, Rust or Java means picking a style or, where
    the shape genuinely differs, adding one -- not editing the extractor.
    """

    style: str = "python"
    #: Statement kinds that introduce a dependency.
    statement_kinds: frozenset[str] = frozenset()
    #: Field holding the module or specifier.
    module_field: str = "module_name"
    #: Field holding imported names (python style).
    name_field: str = "name"
    #: Node kind for a relative module reference (``from ..pkg import x``).
    relative_kinds: frozenset[str] = frozenset()
    #: Node kind marking ``from x import *``.
    wildcard_kinds: frozenset[str] = frozenset()
    #: Node kind for ``x as y``.
    alias_kinds: frozenset[str] = frozenset()
    #: Kinds whose text is a quoted specifier needing the quotes stripped.
    string_kinds: frozenset[str] = frozenset()
    #: Token that marks a type-only import, which creates no runtime coupling.
    type_only_token: str | None = None
    #: Callee names that import dynamically, e.g. ``importlib.import_module``, ``require``.
    dynamic_callees: frozenset[str] = frozenset()
    #: Call node kinds to inspect for dynamic imports.
    call_kinds: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ScopeSpec:
    """How one language introduces scopes and binds names.

    Like :class:`ImportSpec`, this is organised by *style* rather than per language, because
    scoping rules cluster into families: ``python`` (function-scoped, comprehensions get
    their own scope, no block scope) and ``ecmascript`` (block-scoped ``let``/``const``,
    function-scoped ``var``).
    """

    style: str = "python"
    #: Node kind -> the kind of scope it opens.
    scope_kinds: Mapping[str, str] = field(default_factory=dict)
    #: Node kinds that declare a name in the *enclosing* scope.
    declaration_kinds: frozenset[str] = frozenset()
    #: Node kinds holding a callable's parameters.
    parameter_containers: frozenset[str] = frozenset()
    #: Node kinds that bind on the left of an assignment or loop.
    assignment_kinds: frozenset[str] = frozenset()
    #: Node kinds for ``x as y`` bindings (``with``, ``except``, imports).
    alias_kinds: frozenset[str] = frozenset()
    #: Statements that rebind a name to an outer scope.
    rebinding_kinds: frozenset[str] = frozenset()
    #: The receiver's name where the language does not pass it as a parameter -- ``this``
    #: in Java, TypeScript and JavaScript. Empty where the receiver *is* parameter zero
    #: (Python's ``self``, Rust's ``&self``), which is the case the binder handles by
    #: reading the name from the source rather than assuming one.
    #:
    #: It also decides whether parameter zero is a receiver at all. Without it Java's
    #: ``save(String k)`` bound ``k`` as the receiver, so `this.db` matched nothing and
    #: every Java class read as maximally incohesive -- LCOM\* 1.25 on a class with two
    #: clean halves, stamped EXACT.
    implicit_receiver: str = ""
    #: Node kind for a receiver that is written among the parameters but is not one --
    #: Rust's ``&self``. Where it is set, a callable is a method exactly when it has such a
    #: node, which is what tells `fn save(&self)` from the associated `fn new(cfg)`.
    self_parameter_kind: str = ""
    #: Decorators that mean "no receiver despite sitting in a class" -- Python's
    #: ``@staticmethod``. Matched as substrings, since a decorator may be written qualified.
    static_markers: frozenset[str] = frozenset()
    #: True where a member may be written without its receiver -- Java's `vets.findAll()`
    #: for `this.vets.findAll()`. Reading only the qualified form left Spring-style code
    #: measuring nothing: `VetControllerTests` reported LCOM* 1.125, above 1, which is this
    #: project's own signal for "no method touches any field" -- and stamped it EXACT.
    #:
    #: It costs an L0 lookup per candidate rather than a text match, because a parameter or
    #: local of the same name shadows the field and must not be counted as one.
    bare_field_access: bool = False
    #: Node kind for attribute access, used to find ``self.x`` field writes.
    attribute_kind: str = "attribute"
    #: Field on the attribute node holding the receiver.
    attribute_object_field: str = "object"
    #: Field holding the attribute name.
    attribute_name_field: str = "attribute"
    #: Node kind of a plain identifier.
    identifier_kind: str = "identifier"


@dataclass(frozen=True)
class MetricSpec:
    """All metric tables for one language."""

    cyclomatic: CyclomaticSpec = field(default_factory=CyclomaticSpec)
    cognitive: CognitiveSpec = field(default_factory=CognitiveSpec)
    size: SizeSpec = field(default_factory=SizeSpec)
    halstead: HalsteadSpec = field(default_factory=HalsteadSpec)
    imports: ImportSpec = field(default_factory=ImportSpec)
    scopes: ScopeSpec = field(default_factory=ScopeSpec)
