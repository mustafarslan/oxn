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
    models: dict[str, ClassModel] = {}

    for class_node, class_name in _iter_classes(root, profile):
        model = ClassModel(name=class_name, bases=_base_names(class_node, profile))
        body = class_node.child_by_field_name(profile.body_field)
        if body is None:
            models[class_name] = model
            continue

        model.fields |= _declared_fields(body, profile, spec)
        method_nodes = list(_iter_methods(body, profile))
        method_names = {name for _, name in method_nodes}

        for method_node, method_name in method_nodes:
            receiver = _receiver_of(method_node, profile, scopes)
            access = MethodAccess(
                name=method_name,
                start_byte=method_node.start_byte,
                end_byte=method_node.end_byte,
            )
            if receiver is not None:
                _collect_accesses(method_node, profile, spec, receiver, access, method_names)
            model.methods[method_name] = access
            if _is_property(method_node, profile):
                model.properties.add(method_name)

        # A field is anything written through the receiver, plus anything declared in the
        # class body. Reads of names in neither are inherited or dynamic.
        for access in model.methods.values():
            model.fields |= access.writes
        for access in model.methods.values():
            unknown = access.reads - model.fields - method_names - model.properties
            model.unresolved_accesses += len(unknown)
            access.reads -= unknown

        models[class_name] = model

    return models


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


def _base_names(class_node: Node, profile: LanguageProfile) -> tuple[str, ...]:
    """Superclass names as written. Resolving them to entities is L1's job."""
    names: list[str] = []
    for field_name in ("superclasses", "type_parameters", "body"):
        if field_name == "body":
            break
        holder = class_node.child_by_field_name(field_name)
        if holder is None:
            continue
        for child in holder.named_children:
            text = _text(child)
            if text and text.isidentifier():
                names.append(text)
    for child in class_node.named_children:
        if child.type in {"class_heritage", "extends_clause", "super_interfaces"}:
            for inner in child.named_children:
                text = _text(inner)
                if text and text.isidentifier():
                    names.append(text)
    return tuple(dict.fromkeys(names))


def _declared_fields(body: Node, profile: LanguageProfile, spec: ScopeSpec) -> set[str]:
    """Names assigned or annotated directly in the class body."""
    fields: set[str] = set()
    for child in body.named_children:
        if profile.unwrap(child).type in profile.function_like:
            continue
        stack = [child]
        while stack:
            node = stack.pop()
            if node.type in spec.assignment_kinds or node.type == "field_definition":
                left = (
                    node.child_by_field_name("left")
                    or node.child_by_field_name("name")
                    or node.child_by_field_name("property")
                )
                if left is not None and left.type == spec.identifier_kind:
                    fields.add(_text(left))
            if node.type in {"public_field_definition", "property_signature"}:
                name = node.child_by_field_name("name")
                if name is not None:
                    fields.add(_text(name))
            stack.extend(node.named_children)
    return fields


def _receiver_of(method: Node, profile: LanguageProfile, scopes: ScopeTree) -> str | None:
    """The receiver name for this method, from the scope tree L0 already built."""
    for scope in scopes.all_scopes():
        if scope.start_byte == method.start_byte and scope.end_byte == method.end_byte:
            return scope.receiver
    return None


def _collect_accesses(
    method: Node,
    profile: LanguageProfile,
    spec: ScopeSpec,
    receiver: str,
    access: MethodAccess,
    method_names: set[str],
) -> None:
    """Record every ``receiver.name`` in the method, classified as read, write or call."""
    call_kinds = profile.metrics.cognitive.call_kinds
    stack = [method]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type != spec.attribute_kind:
            continue

        obj = node.child_by_field_name(spec.attribute_object_field)
        name_node = node.child_by_field_name(spec.attribute_name_field)
        if obj is None or name_node is None or _text(obj) != receiver:
            continue

        name = _text(name_node)
        if not name:
            continue

        parent = node.parent
        if parent is not None and parent.type in call_kinds and name in method_names:
            access.calls.add(name)
            continue
        if _is_write_target(node, spec):
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
