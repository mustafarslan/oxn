"""Cognitive Complexity (Campbell / SonarSource, white paper v1.7, 29 August 2023).

Implemented from the published specification. Independent reimplementations are
well-precedented -- gocognit, complexipy, rust-code-analysis, eslint-plugin-sonarjs -- and
OXN's per-language increment tables live in :class:`~oxn.profiles.spec.CognitiveSpec`.

The value OXN adds over those implementations is the :class:`Increment` trail: every point
of the score carries the line and the reason that produced it, so a violation reported to a
coding agent reads "+3 at line 42: `if` nested 2 deep" rather than "your score is 14".
That explanation payload is the product; the number alone is not actionable.

Three rules are where reimplementations diverge, all verified against real grammars:

* ``else``/``elif`` are **hybrid** -- ``+1``, no nesting increment, but they *do* raise the
  nesting level. The mental cost was already paid reading the ``if``.
* A ``switch`` and all its cases together incur **one** increment, and a ``catch`` is ``+1``
  however many exception types it names. ``try`` and ``finally`` are ignored entirely.
* Boolean operators score **one increment per run of like operators**, not one per operator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from tree_sitter import Node

    from oxn.profiles.base import LanguageProfile
    from oxn.profiles.spec import CognitiveSpec


@dataclass(frozen=True, slots=True)
class Increment:
    """One contribution to the score, with the reason it was assessed."""

    line: int
    amount: int
    kind: str
    reason: str

    def __str__(self) -> str:
        return f"+{self.amount} at line {self.line}: {self.reason}"


@dataclass
class CognitiveResult:
    """A score and the trail that produced it."""

    score: int = 0
    increments: list[Increment] = field(default_factory=list)
    #: True when the recursion increment could only be assessed for *direct* self-calls.
    #: The specification counts every method in a recursion cycle, which needs a call graph.
    recursion_is_approximate: bool = True

    def add(self, node: Node, amount: int, kind: str, reason: str) -> None:
        if amount:
            self.score += amount
            self.increments.append(Increment(node.start_point[0] + 1, amount, kind, reason))

    def explain(self) -> list[str]:
        return [str(inc) for inc in self.increments]


def cognitive_complexity(
    node: Node, profile: LanguageProfile, *, function_name: str | None = None
) -> CognitiveResult:
    """Score one function body.

    ``function_name`` enables the direct-recursion increment. Indirect recursion needs the
    call graph and is therefore not assessed; the result is marked approximate.
    """
    spec = profile.metrics.cognitive
    context = _Context(
        profile=profile,
        spec=spec,
        result=CognitiveResult(),
        counted_bools=set(),
        function_name=function_name,
    )

    declarative = spec.js_declarative_function_exception and _is_declarative(node, spec)
    decorator_shaped = spec.python_decorator_exception and _is_decorator_shaped(node, profile)

    _walk(context, node, nesting=0, suppress_nesting=declarative or decorator_shaped)
    return context.result


@dataclass(frozen=True, slots=True)
class _Context:
    """What stays the same for an entire scoring run.

    Everything here is invariant through the recursion -- the profile, the tables, the
    accumulator, the enclosing function's name -- so threading it as five separate
    arguments through five mutually recursive functions bought nothing and cost a great
    deal: `_walk` alone re-listed eight arguments at nine call sites.

    Frozen guards rebinding, not contents: `result` and `counted_bools` are accumulators
    and are meant to be mutated.
    """

    profile: LanguageProfile
    spec: CognitiveSpec
    result: CognitiveResult
    counted_bools: set[int]
    function_name: str | None


@dataclass(frozen=True, slots=True)
class _Site:
    """One child under consideration, and the nesting state it is seen at."""

    node: Node
    kind: str
    nesting: int
    suppress_nesting: bool


def _walk(context: _Context, node: Node, *, nesting: int, suppress_nesting: bool) -> None:
    """Score every child of ``node``, dispatching each to the rule that claims it.

    The specification classifies constructs into increment classes, and this loop is that
    classification made executable: `_HANDLERS` holds one function per class, tried in
    order, and the first to claim a child consumes it. A child no rule claims falls through
    to the two increments that are about the *expression* rather than the statement --
    boolean sequences and direct recursion -- and is then descended into.

    Order is load-bearing and matches the chain this replaced. `_ignored` must precede
    everything (a `try` scores nothing but its contents count), and `_else_if` must precede
    `_structural` or a wrapped `else if` is scored twice.
    """
    for child in node.named_children:
        target = context.profile.unwrap(child)
        site = _Site(target, target.type, nesting, suppress_nesting)

        if any(claim(context, site) for claim in _HANDLERS):
            continue

        _score_expression(context, site)
        _walk(context, target, nesting=nesting, suppress_nesting=suppress_nesting)


# ---- one handler per increment class -------------------------------------------------
#
# Each returns True when it has consumed the child. They read as the table in
# `CognitiveSpec`'s docstring, which is the point: the spec says there are four increment
# classes plus a handful of special shapes, and now so does the code.


def _ignored(context: _Context, site: _Site) -> bool:
    """`try`/`finally` score nothing and do not nest, but their contents count."""
    if site.kind not in context.spec.ignored:
        return False
    _walk(context, site.node, nesting=site.nesting, suppress_nesting=site.suppress_nesting)
    return True


def _else_if(context: _Context, site: _Site) -> bool:
    """`else if` where the grammar spells it as an else_clause wrapping an if_statement."""
    spec = context.spec
    wrapped = spec.else_if_via_else_clause or spec.alternative_style == "wrapped"
    if not wrapped or site.kind != spec.else_clause_kind:
        return False
    inner = _sole_if(site.node, spec)
    if inner is None:
        return False
    context.result.add(site.node, 1, "hybrid", "`else if` (hybrid: no nesting increment)")
    # The `else if` is not nesting-incremented, but its body is one level deeper --
    # matching Python's `elif_clause`, and the white paper's `toRegexp` example, where an
    # `if` inside an `else if` body scores "+3 (nesting = 2)" beneath an outer `if`.
    _descend_conditional(
        context, inner, nesting=site.nesting + 1, suppress_nesting=site.suppress_nesting
    )
    return True


def _comprehension(context: _Context, site: _Site) -> bool:
    if site.kind not in context.spec.comprehension_kinds:
        return False
    _score_comprehension(
        context, site.node, nesting=site.nesting, suppress_nesting=site.suppress_nesting
    )
    return True


def _structural(context: _Context, site: _Site) -> bool:
    """B1 + B2 + B3: increments, raises nesting, and receives the nesting increment."""
    if site.kind not in context.spec.structural:
        return False
    reason = f"`{_label(site.node)}`" + (f" nested {site.nesting} deep" if site.nesting else "")
    context.result.add(site.node, 1 + site.nesting, "structural", reason)
    _descend_conditional(
        context, site.node, nesting=site.nesting + 1, suppress_nesting=site.suppress_nesting
    )
    return True


def _hybrid(context: _Context, site: _Site) -> bool:
    """`else`/`elif`: +1, no nesting increment received, but nesting is raised."""
    if site.kind not in context.spec.hybrid:
        return False
    # A loop's or a `try`'s `else` is not an `if`/`else` and does not count.
    parent = site.node.parent
    if parent is not None and parent.type not in context.spec.hybrid_parents:
        _walk(context, site.node, nesting=site.nesting, suppress_nesting=site.suppress_nesting)
        return True
    context.result.add(
        site.node, 1, "hybrid", f"`{_label(site.node)}` (hybrid: no nesting increment)"
    )
    _walk(context, site.node, nesting=site.nesting + 1, suppress_nesting=site.suppress_nesting)
    return True


def _labelled_jump(context: _Context, site: _Site) -> bool:
    """Only a *labelled* or multi-level jump increments. A bare `break` does not."""
    spec = context.spec
    if site.kind not in spec.labelled_jump_kinds:
        return False
    if any(child.type in spec.jump_label_kinds for child in site.node.named_children):
        context.result.add(site.node, 1, "fundamental", f"labelled `{_label(site.node)}`")
    return True


def _fundamental(context: _Context, site: _Site) -> bool:
    """+1, and neither raises nor receives a nesting increment."""
    if site.kind not in context.spec.fundamental:
        return False
    context.result.add(site.node, 1, "fundamental", f"`{_label(site.node)}`")
    _walk(context, site.node, nesting=site.nesting, suppress_nesting=site.suppress_nesting)
    return True


def _nesting_only(context: _Context, site: _Site) -> bool:
    """No increment of its own, but everything inside is one level deeper.

    Unless a compensating usage from Appendix A applies -- and the exception must be
    re-evaluated at every level, not decided once at the top: a decorator generator is a
    decorator-shaped function *containing another one*, and both levels are exempt. The
    white paper's `decorator_generator` example scores 1 only if this holds.
    """
    if site.kind not in context.spec.nesting_only:
        return False
    inner = site.nesting if site.suppress_nesting else site.nesting + 1
    exempt = context.spec.python_decorator_exception and _is_decorator_shaped(
        site.node, context.profile
    )
    _walk(context, site.node, nesting=inner, suppress_nesting=exempt)
    return True


#: Tried in order; the first to claim a child consumes it. See `_walk` on why order matters.
_HANDLERS = (
    _ignored,
    _else_if,
    _comprehension,
    _structural,
    _hybrid,
    _labelled_jump,
    _fundamental,
    _nesting_only,
)


def _score_expression(context: _Context, site: _Site) -> None:
    """The two increments that are about an expression rather than a statement."""
    spec = context.spec
    # The operator must be checked, not just the node kind: in TypeScript a
    # `binary_expression` is also `a > 0`, which is not a logical operator at all.
    if (
        site.kind == spec.boolean_node
        and site.node.id not in context.counted_bools
        and _operator_of(site.node) in spec.boolean_operators
    ):
        context.result.add(
            site.node,
            _score_boolean_sequence(site.node, spec, context.counted_bools),
            "fundamental",
            "sequence of binary logical operators",
        )

    name = context.function_name
    if name and site.kind in spec.call_kinds and _is_self_call(site.node, spec, name):
        context.result.add(site.node, 1, "fundamental", f"recursive call to `{name}`")


def _score_comprehension(
    context: _Context, node: Node, *, nesting: int, suppress_nesting: bool
) -> None:
    """Score a comprehension, whose clauses are *siblings* of its body, not its ancestors.

    Tree shape cannot supply the nesting here. The rules, derived by characterising
    complexipy because the specification is silent (docs/metrics.md section 3.2):

    * every ``for`` clause is structural at the comprehension's own nesting -- a second
      ``for`` in the same comprehension is not nested under the first;
    * every filter ``if`` is fundamental, ``+1``, never nesting-incremented;
    * the body sits one level deeper, so a comprehension inside a comprehension nests.

    Verified: ``[x for x in a]`` = 1, ``[x for x in a for y in b if x if y]`` = 4,
    ``[[y for y in x] for x in a]`` = 3.
    """
    spec = context.spec
    loops = [c for c in node.named_children if c.type in spec.comprehension_loop_kinds]
    filters = [c for c in node.named_children if c.type in spec.comprehension_filter_kinds]

    for clause in loops:
        reason = "comprehension `for`" + (f" nested {nesting} deep" if nesting else "")
        context.result.add(clause, 1 + nesting, "structural", reason)
    for clause in filters:
        context.result.add(clause, 1, "fundamental", "comprehension filter `if`")

    inner = nesting + 1 if loops else nesting
    for child in node.named_children:
        at_clause_level = child in loops or child in filters
        _walk(
            context,
            _Wrapper(child),  # type: ignore[arg-type]
            nesting=nesting if at_clause_level else inner,
            suppress_nesting=suppress_nesting,
        )


def _descend_conditional(
    context: _Context, node: Node, *, nesting: int, suppress_nesting: bool
) -> None:
    """Visit a conditional's children, keeping ``else``/``elif`` at the parent's level.

    Fields named in ``same_nesting_fields`` hold the alternative branches. Nesting them
    under their own ``if`` would double-count every ``else if`` chain in a codebase.
    """
    alternatives = [
        child
        for field_name in context.spec.same_nesting_fields
        for child in node.children_by_field_name(field_name)
    ]
    alternative_ids = {child.id for child in alternatives}

    for child in node.named_children:
        if child.id in alternative_ids:
            continue
        _walk(
            context,
            _Wrapper(child),  # type: ignore[arg-type]
            nesting=nesting,
            suppress_nesting=suppress_nesting,
        )

    for child in alternatives:
        _visit_alternative(context, child, nesting=nesting - 1, suppress_nesting=suppress_nesting)


def _visit_alternative(
    context: _Context, node: Node, *, nesting: int, suppress_nesting: bool
) -> None:
    """Score whatever sits in an ``if``'s ``alternative`` slot.

    Three grammar shapes reach here (see :attr:`CognitiveSpec.alternative_style`), and all
    three must produce the same score for the same logic -- which the cross-language
    transliteration tests enforce.
    """
    spec = context.spec
    if spec.alternative_style == "direct":
        if node.type == spec.if_kind:
            # Go and Java: `else if` is the next `if` sitting directly in the slot.
            context.result.add(node, 1, "hybrid", "`else if` (hybrid: no nesting increment)")
            _descend_conditional(
                context, node, nesting=nesting + 1, suppress_nesting=suppress_nesting
            )
            return
        if node.type in spec.plain_else_kinds:
            # A bare block in the slot is a plain `else`, which has no node of its own.
            context.result.add(node, 1, "hybrid", "`else` (hybrid: no nesting increment)")
            _walk(context, node, nesting=nesting + 1, suppress_nesting=suppress_nesting)
            return

    _walk(
        context,
        _Wrapper(node),  # type: ignore[arg-type]
        nesting=nesting,
        suppress_nesting=suppress_nesting,
    )


class _Wrapper:
    """Presents a single node to :func:`_walk`, which iterates ``named_children``."""

    __slots__ = ("named_children",)

    def __init__(self, node: Node) -> None:
        self.named_children = [node]


# ---- boolean sequences ---------------------------------------------------------------


def _score_boolean_sequence(node: Node, spec: CognitiveSpec, counted: set[int]) -> int:
    """One increment per *run* of like operators, recursing into non-boolean leaves.

    ``a && b && c`` is one run, so ``+1``. ``a && b && c || d || e && f`` is three runs, so
    ``+3``. A negation or parenthesis ends a run and starts a fresh sequence context, so
    ``a && !(b && c)`` is ``+2``.
    """
    operators: list[str] = []
    leaves: list[Node] = []
    _flatten_boolean(node, spec, operators, leaves, counted)

    runs = 0
    previous: str | None = None
    for operator in operators:
        if operator != previous:
            runs += 1
            previous = operator

    total = runs
    for leaf in leaves:
        for nested in _find_boolean_nodes(leaf, spec, counted):
            total += _score_boolean_sequence(nested, spec, counted)
    return total


def _flatten_boolean(
    node: Node, spec: CognitiveSpec, operators: list[str], leaves: list[Node], counted: set[int]
) -> None:
    counted.add(node.id)
    operator_node = node.child_by_field_name("operator")
    operator = (
        operator_node.text.decode("utf-8", "replace")
        if operator_node is not None and operator_node.text
        else ""
    )

    for field_name in ("left", "right"):
        side = node.child_by_field_name(field_name)
        if side is None:
            continue
        if side.type == spec.boolean_node and _operator_of(side) in spec.boolean_operators:
            _flatten_boolean(side, spec, operators, leaves, counted)
        else:
            leaves.append(side)
        if field_name == "left":
            operators.append(operator)


def _operator_of(node: Node) -> str:
    operator = node.child_by_field_name("operator")
    return (
        operator.text.decode("utf-8", "replace") if operator is not None and operator.text else ""
    )


def _find_boolean_nodes(node: Node, spec: CognitiveSpec, counted: set[int]) -> list[Node]:
    """Boolean operators inside a sequence leaf -- a new sequence context each."""
    found: list[Node] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if (
            current.type == spec.boolean_node
            and current.id not in counted
            and _operator_of(current) in spec.boolean_operators
        ):
            found.append(current)
            continue
        stack.extend(current.named_children)
    return found


# ---- Appendix A compensating usages ---------------------------------------------------


def _is_decorator_shaped(node: Node, profile: LanguageProfile) -> bool:
    """Python decorators: a function containing *only* a nested function and a return.

    Such a function does not increment the nesting level, so a decorator's inner logic is
    not penalised for the idiom the language requires.
    """
    body = node.child_by_field_name(profile.body_field)
    if body is None:
        return False
    kinds = [child.type for child in body.named_children]
    unwrapped = [
        profile.unwrap(child).type if child.type in profile.wrappers else child.type
        for child in body.named_children
    ]
    has_nested = any(kind in profile.function_like for kind in unwrapped)
    has_return = "return_statement" in kinds
    only_those = all(
        kind in profile.function_like or kind == "return_statement" for kind in unwrapped
    )
    return has_nested and has_return and only_those


def _is_declarative(node: Node, spec: CognitiveSpec) -> bool:
    """JavaScript outer functions used purely as a namespace are ignored.

    The test the specification gives: no statement at the function's *top level* is subject
    to a structural increment. Logic inside a sub-function does not disqualify it.
    """
    body = None
    for child in node.named_children:
        if child.type in {"statement_block", "block"}:
            body = child
            break
    if body is None:
        return False
    return not any(child.type in spec.structural for child in body.named_children)


# ---- helpers --------------------------------------------------------------------------


def _sole_if(else_clause: Node, spec: CognitiveSpec) -> Node | None:
    """The ``if_statement`` an ``else_clause`` wraps, if that is all it holds."""
    named = else_clause.named_children
    if len(named) == 1 and named[0].type == spec.if_kind:
        return named[0]
    return None


def _is_self_call(node: Node, spec: CognitiveSpec, function_name: str) -> bool:
    callee = node.child_by_field_name(spec.callee_field)
    if callee is None or callee.text is None:
        return False
    return callee.text.decode("utf-8", "replace") == function_name


def _label(node: Node) -> str:
    """A readable name for a node kind, for the explanation trail."""
    return node.type.replace("_statement", "").replace("_clause", "").replace("_expression", "")
