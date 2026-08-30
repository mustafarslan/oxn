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

    #: True when the grammar expresses ``else if`` as an ``else_clause`` wrapping an
    #: ``if_statement`` (TypeScript, JavaScript) rather than a dedicated node (Python's
    #: ``elif_clause``). The pair then counts as *one* hybrid increment, not two.
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
    #: True when a bare string expression statement is documentation rather than data.
    docstrings_are_comments: bool = False


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
class MetricSpec:
    """All metric tables for one language."""

    cyclomatic: CyclomaticSpec = field(default_factory=CyclomaticSpec)
    cognitive: CognitiveSpec = field(default_factory=CognitiveSpec)
    size: SizeSpec = field(default_factory=SizeSpec)
    halstead: HalsteadSpec = field(default_factory=HalsteadSpec)
