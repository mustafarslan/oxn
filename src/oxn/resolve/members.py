"""Class member access: which method touches which field, and which calls which.

This is the substrate every cohesion metric reads. Getting it right depends on one thing
the scope resolver already established: **the receiver is whatever the first parameter is
actually called**, not an assumed ``self``. A codebase spelling it ``cls``, ``this`` or
``me`` would otherwise report every class as maximally incohesive.

What cannot be recovered here, and is counted rather than guessed at:

* **inherited fields** -- a subclass touching only its base's state looks incohesive until
  the base is known. Resolved through in-repo ``EXTENDS`` edges where possible;
* **dynamic attributes** -- ``setattr``, ``__dict__``, ``Object.assign``. Invisible at any
  resolution level, which is why LCOM is stamped APPROX whenever they might apply.

Unresolvable accesses are counted in :attr:`ClassModel.unresolved_accesses`, the same
honesty valve as ``unresolved_imports``: a number a consumer can weigh, not a silent drop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator

    from tree_sitter import Node

    from oxn.profiles.base import LanguageProfile
    from oxn.profiles.spec import ScopeSpec
    from oxn.resolve.scopes import ScopeTree


@dataclass
class MethodAccess:
    """What one method touches."""

    name: str
    reads: set[str] = field(default_factory=set)
    writes: set[str] = field(default_factory=set)
    #: Sibling methods called on the receiver -- the edges that separate LCOM4 from LCOM3.
    calls: set[str] = field(default_factory=set)
    #: The name these accesses came through (`self`, `this`, `c`). Kept because every entry
    #: in `calls` is by construction a *receiver* call, and `resolve_call` must be told so:
    #: the calling file's own top-level declarations are not evidence about a receiver.
    receiver: str = ""
    start_byte: int = 0
    end_byte: int = 0

    @property
    def touches(self) -> set[str]:
        return self.reads | self.writes


@dataclass
class ClassModel:
    """A class, its fields, and how its methods use them."""

    name: str
    fields: set[str] = field(default_factory=set)
    methods: dict[str, MethodAccess] = field(default_factory=dict)
    bases: tuple[str, ...] = ()
    #: Receiver-qualified accesses naming nothing this class declares -- inherited or
    #: dynamic. Counted, never silently dropped.
    unresolved_accesses: int = 0
    #: Names accessed through the receiver that resolve to a property, i.e. a call
    #: disguised as a field read.
    properties: set[str] = field(default_factory=set)

    @property
    def is_approximate(self) -> bool:
        """True when something was touched that this class cannot account for."""
        return self.unresolved_accesses > 0

    def method_pairs(self) -> list[tuple[str, str]]:
        names = sorted(self.methods)
        return [
            (first, second) for index, first in enumerate(names) for second in names[index + 1 :]
        ]


def build_class_models(
    root: Node, profile: LanguageProfile, scopes: ScopeTree
) -> dict[str, ClassModel]:
    """Extract the member-access model for every class in a file."""
    spec = profile.metrics.scopes
    return {
        class_name: _one_class(class_node, class_name, profile, scopes, spec)
        for class_node, class_name in _iter_classes(root, profile)
    }


def _one_class(
    class_node: Node,
    class_name: str,
    profile: LanguageProfile,
    scopes: ScopeTree,
    spec: ScopeSpec,
) -> ClassModel:
    """One class: what it declares, what its methods touch, and what it inherits."""
    model = ClassModel(name=class_name, bases=_base_names(class_node, profile))
    body = class_node.child_by_field_name(profile.body_field)
    if body is None:
        return model

    model.fields |= _declared_fields(body, profile, spec)
    method_nodes = list(_iter_methods(body, profile))
    method_names = {name for _, name in method_nodes}

    context = _Members(profile, scopes, spec, method_names)
    for method_node, method_name in method_nodes:
        model.methods[method_name] = _method_access(context, method_node, method_name)
        if _is_property(method_node, profile):
            model.properties.add(method_name)

    _settle_fields(model, method_names)
    return model


@dataclass(frozen=True, slots=True)
class _Members:
    """What every method of one class is read against: the tables, the scopes, the siblings."""

    profile: LanguageProfile
    scopes: ScopeTree
    spec: ScopeSpec
    #: The class's own method names, so a call to a sibling is not mistaken for a field.
    method_names: set[str]


def _method_access(context: _Members, method_node: Node, method_name: str) -> MethodAccess:
    """What one method reads and writes through its receiver.

    Without a receiver there is nothing to attribute an access *to*, so the method is
    recorded with an empty access set rather than guessed at: LCOM over invented accesses
    is worse than LCOM over none.
    """
    access = MethodAccess(
        name=method_name, start_byte=method_node.start_byte, end_byte=method_node.end_byte
    )
    receiver = _receiver_of(method_node, context.profile, context.scopes)
    if receiver is not None:
        access.receiver = receiver
        _collect_accesses(context, method_node, receiver, access)
    return access


def _settle_fields(model: ClassModel, method_names: set[str]) -> None:
    """A field is anything written through the receiver, plus anything the body declares.

    Reads of names in neither are inherited or dynamic. They are counted -- an unresolved
    access is exactly the uncertainty the exactness stamp exists to report -- and then
    dropped, so cohesion is computed over members this class actually has.
    """
    for access in model.methods.values():
        model.fields |= access.writes
    for access in model.methods.values():
        unknown = access.reads - model.fields - method_names - model.properties
        model.unresolved_accesses += len(unknown)
        access.reads -= unknown


def _iter_classes(root: Node, profile: LanguageProfile) -> Iterator[tuple[Node, str]]:
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        target = profile.unwrap(node)
        if target.type in profile.class_like:
            name = profile.entity_name(target)
            if name:
                yield target, name


def _iter_methods(body: Node, profile: LanguageProfile) -> Iterator[tuple[Node, str]]:
    """Direct method members of a class body, not functions nested inside them."""
    for child in body.named_children:
        target = profile.unwrap(child)
        if target.type in profile.function_like:
            name = profile.entity_name(target)
            if name:
                yield target, name


#: Grammar nodes that hold a class's supertypes when they are not in a named field.
_HERITAGE_KINDS = frozenset({"class_heritage", "extends_clause", "super_interfaces"})

#: Field declarations whose name lives in a `name` field rather than left of an assignment.
_NAMED_FIELD_KINDS = frozenset({"public_field_definition", "property_signature"})


def _base_names(class_node: Node, profile: LanguageProfile) -> tuple[str, ...]:
    """Superclass names as written. Resolving them to entities is L1's job.

    Two grammar shapes: Python and Java put supertypes in a named field, TypeScript hangs
    them off a heritage clause. Both are read, and duplicates collapse.
    """
    names: list[str] = []
    for field_name in ("superclasses", "type_parameters"):
        holder = class_node.child_by_field_name(field_name)
        if holder is not None:
            names.extend(_plain_names(holder))
    for child in class_node.named_children:
        if child.type in _HERITAGE_KINDS:
            names.extend(_plain_names(child))
    return tuple(dict.fromkeys(names))


def _plain_names(holder: Node) -> Iterator[str]:
    """Direct children that are bare identifiers, skipping generics and expressions."""
    for child in holder.named_children:
        text = _text(child)
        if text and text.isidentifier():
            yield text


def _declared_fields(body: Node, profile: LanguageProfile, spec: ScopeSpec) -> set[str]:
    """Names assigned or annotated directly in the class body, methods excluded."""
    fields: set[str] = set()
    for child in body.named_children:
        if profile.unwrap(child).type not in profile.function_like:
            fields |= _field_names_in(child, spec)
    return fields


def _field_names_in(child: Node, spec: ScopeSpec) -> set[str]:
    """Every field name declared anywhere under one class-body statement."""
    found: set[str] = set()
    stack = [child]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        name = _field_name(node, spec)
        if name:
            found.add(name)
    return found


def _field_name(node: Node, spec: ScopeSpec) -> str:
    """The field this node declares, or an empty string when it declares none."""
    if node.type in _NAMED_FIELD_KINDS:
        name = node.child_by_field_name("name")
        return _text(name) if name is not None else ""
    if node.type not in spec.assignment_kinds and node.type != "field_definition":
        return ""
    left = (
        node.child_by_field_name("left")
        or node.child_by_field_name("name")
        or node.child_by_field_name("property")
    )
    return _text(left) if left is not None and left.type == spec.identifier_kind else ""


def _receiver_of(method: Node, profile: LanguageProfile, scopes: ScopeTree) -> str | None:
    """The receiver name for this method, from the scope tree L0 already built."""
    for scope in scopes.all_scopes():
        if scope.start_byte == method.start_byte and scope.end_byte == method.end_byte:
            return scope.receiver
    return None


def _collect_accesses(context: _Members, method: Node, receiver: str, access: MethodAccess) -> None:
    """Record every ``receiver.name`` in the method, classified as read, write or call."""
    stack = [method]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        name = _receiver_attribute(node, context.spec, receiver)
        if name:
            _classify_access(context, node, name, access)


def _receiver_attribute(node: Node, spec: ScopeSpec, receiver: str) -> str:
    """The attribute name when this node is `receiver.name`, else an empty string.

    The receiver is whatever the method's first parameter is actually called -- read from
    the scope tree, never assumed to be `self`, which is the crux of correct LCOM
    (docs/metrics.md 6.1).
    """
    if node.type != spec.attribute_kind:
        return ""
    obj = node.child_by_field_name(spec.attribute_object_field)
    name_node = node.child_by_field_name(spec.attribute_name_field)
    if obj is None or name_node is None or _text(obj) != receiver:
        return ""
    return _text(name_node)


def _classify_access(context: _Members, node: Node, name: str, access: MethodAccess) -> None:
    """A call to a sibling method, a write to a field, or a read of one.

    The call case is checked first and only for *siblings*: `self.helper()` where `helper`
    is a method of this class is cohesion evidence of a different kind from `self.helper`
    as a field, and LCOM4 joins on it.
    """
    parent = node.parent
    call_kinds = context.profile.metrics.cognitive.call_kinds
    if parent is not None and parent.type in call_kinds and name in context.method_names:
        access.calls.add(name)
    elif _is_write_target(node, context.spec):
        access.writes.add(name)
    else:
        access.reads.add(name)


def _is_write_target(node: Node, spec: ScopeSpec) -> bool:
    """True when this attribute is the left side of an assignment."""
    parent = node.parent
    if parent is None or parent.type not in spec.assignment_kinds:
        return False
    left = parent.child_by_field_name("left") or parent.child_by_field_name("name")
    return left is not None and left.start_byte == node.start_byte


def _is_property(method: Node, profile: LanguageProfile) -> bool:
    """A ``@property`` access reads like a field but is really a call (metrics.md 6.1)."""
    parent = method.parent
    if parent is None or parent.type not in profile.wrappers:
        return False
    return any(
        "property" in _text(child) for child in parent.named_children if child.type == "decorator"
    )


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace") if node.text else ""
