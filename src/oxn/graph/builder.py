"""Build the containment skeleton of the code graph from a parse tree.

One walk, profile-driven, producing :class:`~oxn.graph.model.Entity` rows. The two rules
that matter and that P2's metrics inherit for free:

* **Wrappers are transparent.** A decorated Python function or an exported TS class is one
  entity, spanning the wrapper (so decorator lines belong to the function) but named and
  shaped by the inner definition.
* **Nested definitions are their own entities, and the walk does not fold them into the
  parent.** This is exactly the "do not descend into nested functions" rule cyclomatic
  complexity needs -- establishing it in the skeleton means P2 does not restate it.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from oxn.graph.model import Entity, EntityKind, ParsedFile, entity_id

if TYPE_CHECKING:  # pragma: no cover
    from tree_sitter import Node

    from oxn.profiles.base import LanguageProfile

#: Node kinds that mean "method" rather than "function" when they appear inside a type.
_METHOD_KINDS = frozenset({"method_definition", "method_signature", "abstract_method_signature"})
_ANONYMOUS_KINDS = frozenset({"lambda", "arrow_function", "function_expression"})
_INTERFACE_KINDS = frozenset({"interface_declaration", "type_alias_declaration"})


def content_sha(source: bytes) -> str:
    """The cache key half that depends on file content."""
    return hashlib.blake2b(source, digest_size=32).hexdigest()


def build_file(path: str, source: bytes, profile: LanguageProfile, tree_root: Node) -> ParsedFile:
    """Walk one parse tree into the graph's containment skeleton."""
    entities: list[Entity] = []
    occurrences: dict[tuple[EntityKind, str], int] = {}

    def new_entity(
        kind: EntityKind,
        name: str | None,
        qualified_name: str,
        node: Node,
        parent_id: str | None,
        attrs: dict[str, object],
        span: Node | None = None,
    ) -> Entity:
        key = (kind, qualified_name)
        occurrence = occurrences.get(key, 0)
        occurrences[key] = occurrence + 1
        outer = span or node
        ent = Entity(
            id=entity_id(path, kind, qualified_name, occurrence),
            file_path=path,
            kind=kind,
            name=name,
            qualified_name=qualified_name,
            start_byte=outer.start_byte,
            end_byte=outer.end_byte,
            start_line=outer.start_point[0] + 1,
            end_line=outer.end_point[0] + 1,
            parent_id=parent_id,
            attrs=dict(attrs),
        )
        entities.append(ent)
        return ent

    module_name = _module_name(path)
    module = new_entity(
        EntityKind.MODULE, module_name, module_name, tree_root, None, {"language": profile.name}
    )

    def classify(node: Node, inside_type: bool) -> EntityKind:
        if node.type in profile.class_like:
            return EntityKind.INTERFACE if node.type in _INTERFACE_KINDS else EntityKind.CLASS
        if node.type in _ANONYMOUS_KINDS:
            return EntityKind.LAMBDA
        if node.type in _METHOD_KINDS or inside_type:
            return EntityKind.METHOD
        return EntityKind.FUNCTION

    def visit(node: Node, parent: Entity, prefix: str, inside_type: bool) -> None:
        for child in node.named_children:
            definition = profile.unwrap(child)
            if not profile.is_definition(definition):
                visit(child, parent, prefix, inside_type)
                continue

            kind = classify(definition, inside_type)
            name = profile.entity_name(definition)
            local = name or f"<{definition.type}@{definition.start_point[0] + 1}>"
            qualified = f"{prefix}.{local}" if prefix else local

            attrs: dict[str, object] = {"node_kind": definition.type}
            if kind in {EntityKind.FUNCTION, EntityKind.METHOD, EntityKind.LAMBDA}:
                attrs["parameters"] = profile.parameter_names(definition)
            if child is not definition:
                attrs["wrapped_by"] = child.type
            if _is_abstract(definition, profile):
                attrs["is_abstract"] = True

            # The wrapper carries the span so decorator lines belong to the entity; the
            # definition carries everything else.
            entity = new_entity(kind, name, qualified, definition, parent.id, attrs, span=child)

            body = definition.child_by_field_name(profile.body_field)
            if body is not None:
                visit(
                    body,
                    entity,
                    qualified,
                    inside_type=kind in {EntityKind.CLASS, EntityKind.INTERFACE},
                )
            else:
                # Arrow functions and lambdas have expression bodies, not a body field.
                for grandchild in definition.named_children:
                    visit(grandchild, entity, qualified, inside_type=False)

    visit(tree_root, module, module_name, inside_type=False)

    return ParsedFile(
        path=path,
        language=profile.name,
        content_sha=content_sha(source),
        size_bytes=len(source),
        parse_incomplete=tree_root.has_error,
        entities=tuple(entities),
    )


def _is_abstract(node: Node, profile: LanguageProfile) -> bool:
    """Syntax-level abstractness only. The semantic rules land with Martin metrics in P4."""
    if node.type in profile.abstract_kinds:
        return True
    if not profile.abstract_markers:
        return False
    return any(
        child.type in profile.abstract_markers
        or (child.text and child.text.decode("utf-8", "replace") in profile.abstract_markers)
        for child in node.children
        if not child.is_named
    )


def _module_name(path: str) -> str:
    """Provisional module naming: the path without its extension, dots for separators.

    Real module resolution -- ``tsconfig`` paths, ``go.mod`` prefixes, Python package roots
    -- is P4 work (docs/metrics.md section 4.1). This is deliberately a placeholder, and the
    graph records it as such.
    """
    from pathlib import PurePosixPath

    p = PurePosixPath(path)
    parts = [*p.parent.parts, p.stem] if str(p.parent) != "." else [p.stem]
    return ".".join(part for part in parts if part not in {"", "/"})
