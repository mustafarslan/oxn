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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from oxn.graph.model import Entity, EntityKind, ParsedFile, entity_id
from oxn.profiles.base import implemented_type, receiver_type

if TYPE_CHECKING:  # pragma: no cover
    from tree_sitter import Node

    from oxn.profiles.base import LanguageProfile

#: Node kinds that mean "method" rather than "function" wherever they appear. Go's two
#: spellings for an interface's method are here rather than resting on `inside_type`,
#: because a Go interface is a `type_spec` holding its members under `type` and not under
#: `body`, so the descent never marks them as inside anything. Left as functions, the gate
#: read every Go interface at NOM 0 while `oxn classes` read the real count.
_METHOD_KINDS = frozenset(
    {
        "method_definition",
        "method_signature",
        "abstract_method_signature",
        "method_elem",
        "method_spec",
    }
)
#: Anonymous callables, in every language that has them. Go's `func_literal` and Rust's
#: `closure_expression` were missing, so both read as ordinary functions -- and Rust's took
#: the name of whatever it was bound to, which put `let write = |k| ...` into the *named*
#: function population as `write`. Gating does not change (`lambda` is a callable kind
#: either way); what changes is that the label is now true.
_ANONYMOUS_KINDS = frozenset(
    {
        "lambda",
        "arrow_function",
        "function_expression",
        "func_literal",
        "closure_expression",
        # The last two languages to disagree with the rest, found the same way Go's and
        # Rust's were: by asking every profile which of its `function_like` kinds can carry
        # no name of its own. Java's `x -> x` and ECMAScript's `function* () {}` were both
        # sitting in the *named function* population, which is the population two languages
        # get compared on.
        "lambda_expression",
        "generator_function",
    }
)
#: A declaration that is an interface by its own node kind. Rust's `trait` is one: it
#: declares a contract and holds no state, which is what the label means, and leaving it out
#: put every Rust trait in the God-Class population rather than the interface-segregation one.
_INTERFACE_KINDS = frozenset({"interface_declaration", "type_alias_declaration", "trait_item"})


def _kind_attrs(definition: Node, kind: EntityKind, profile: LanguageProfile) -> dict[str, object]:
    """What an entity records because of *what it is*, rather than where it sits.

    Both entries are carried rather than resolved, and for one reason: the other half of each
    is in a sibling file that this tree cannot see. A Go method names its receiver's type and
    a Rust `impl` block names the type it belongs to, and either may be declared elsewhere in
    the package -- `metrics.coupling.package_class_totals` joins them once every file is in.

    Module-level because `_Skeleton` is at its own `weighted_methods_per_class` ceiling, and
    adding the second of these to it is what said so.
    """
    if kind in {EntityKind.FUNCTION, EntityKind.METHOD, EntityKind.LAMBDA}:
        receiver = receiver_type(definition, profile.receiver_field)
        parameters: dict[str, object] = {"parameters": profile.parameter_names(definition)}
        return parameters if receiver is None else {**parameters, "receiver_type": receiver}
    if kind in {EntityKind.CLASS, EntityKind.INTERFACE}:
        implemented = implemented_type(definition, profile.implements_field)
        return {"implements": implemented} if implemented else {}
    return {}


#: Kinds a class-like node may *hold* to make it an interface. Go writes `type R interface
#: {...}` as a `type_spec` whose `type` child says which it is, so the node kind alone
#: cannot tell a struct from an interface -- and the class ceilings record an interface as a
#: finding of its own, an interface-segregation smell rather than a God Class.
_INTERFACE_HOLDERS = frozenset({"interface_type"})


def _is_interface(definition: Node) -> bool:
    """True when a class-like node declares an interface rather than a class."""
    if definition.type in _INTERFACE_KINDS:
        return True
    holder = definition.child_by_field_name("type")
    return holder is not None and holder.type in _INTERFACE_HOLDERS


def content_sha(source: bytes) -> str:
    """The cache key half that depends on file content."""
    return hashlib.blake2b(source, digest_size=32).hexdigest()


def build_file(path: str, source: bytes, profile: LanguageProfile, tree_root: Node) -> ParsedFile:
    """Walk one parse tree into the graph's containment skeleton."""
    skeleton = _Skeleton(path=path, profile=profile)
    return skeleton.build(source, tree_root)


@dataclass(frozen=True, slots=True)
class _NewEntity:
    """What an entity *is*, separately from where in the tree it sits.

    The two were seven positional arguments to one function, which the gate rejected at a
    ceiling of five -- and rightly: "kind, name, qualified name, node, parent, attrs, span"
    is two ideas wearing one signature.
    """

    kind: EntityKind
    name: str | None
    qualified_name: str
    parent_id: str | None
    attrs: dict[str, object]


@dataclass
class _Skeleton:
    """The containment walk, and the four things it threads through every step.

    Three closures over `path`, `profile`, `entities` and `occurrences` used to live inside
    `build_file`, which made it the most complex function in the project at 37 -- a nested
    function raises the nesting level of everything inside it, so the whole walk was scored
    two and three levels deep for reasons that were about scoping rather than about the
    logic. The state is the walk, so it gets an object, and each phase gets a name.
    """

    path: str
    profile: LanguageProfile
    entities: list[Entity] = field(default_factory=list)
    #: (kind, qualified name) -> how many have been seen, so same-named siblings -- two
    #: lambdas on one line, an overload set -- get distinct ids.
    occurrences: dict[tuple[EntityKind, str], int] = field(default_factory=dict)

    def build(self, source: bytes, tree_root: Node) -> ParsedFile:
        module_name = _module_name(self.path)
        module = self._add(
            _NewEntity(
                kind=EntityKind.MODULE,
                name=module_name,
                qualified_name=module_name,
                parent_id=None,
                attrs={"language": self.profile.name},
            ),
            tree_root,
        )
        self._visit(tree_root, module, module_name, inside_type=False)
        return ParsedFile(
            path=self.path,
            language=self.profile.name,
            content_sha=content_sha(source),
            size_bytes=len(source),
            parse_incomplete=tree_root.has_error,
            entities=tuple(self.entities),
        )

    def _add(self, new: _NewEntity, span: Node) -> Entity:
        """Record one entity, numbering same-named siblings as it goes.

        `span` is the range the entity claims, which is the *wrapper* for a decorated
        definition so the decorator lines belong to it.
        """
        key = (new.kind, new.qualified_name)
        occurrence = self.occurrences.get(key, 0)
        self.occurrences[key] = occurrence + 1
        entity = Entity(
            id=entity_id(self.path, new.kind, new.qualified_name, occurrence),
            file_path=self.path,
            kind=new.kind,
            name=new.name,
            qualified_name=new.qualified_name,
            start_byte=span.start_byte,
            end_byte=span.end_byte,
            start_line=span.start_point[0] + 1,
            end_line=span.end_point[0] + 1,
            parent_id=new.parent_id,
            attrs=dict(new.attrs),
        )
        self.entities.append(entity)
        return entity

    def _classify(self, node: Node, inside_type: bool, name: str | None) -> EntityKind:
        if node.type in self.profile.class_like:
            return EntityKind.INTERFACE if _is_interface(node) else EntityKind.CLASS
        if node.type in _ANONYMOUS_KINDS:
            # A named callable declared in a class body is a method, whatever node kind the
            # grammar spells it with. `handler = (x) => x` is how a modern TypeScript or
            # JavaScript class writes one, and reading it as a lambda made both class
            # ceilings inert against the shape: four methods rewritten as four field arrows
            # went from NOM 4 / WMC 8.0 to NOM 0 / WMC 0.0, and both are gated. Java, whose
            # `Runnable a = () -> {}` is not an anonymous *kind*, has always counted it.
            # `name` and not `inside_type` alone, so `x = [lambda: 1]` in a class body -- a
            # callable nothing can call by name -- stays the lambda it is.
            return EntityKind.METHOD if inside_type and name else EntityKind.LAMBDA
        if node.type in _METHOD_KINDS or inside_type:
            return EntityKind.METHOD
        # A declared receiver makes it a method wherever the grammar puts it. Without this
        # every Go method was a `function`, so `_class_totals` skipped it and both class
        # ceilings were silently inert for the language.
        if receiver_type(node, self.profile.receiver_field) is not None:
            return EntityKind.METHOD
        return EntityKind.FUNCTION

    def _visit(self, node: Node, parent: Entity, prefix: str, inside_type: bool) -> None:
        """Every child of `node`, considered in turn. `node` itself is not reconsidered."""
        for child in node.named_children:
            self._consider(child, parent, prefix, inside_type)

    def _consider(self, child: Node, parent: Entity, prefix: str, inside_type: bool) -> None:
        """One node: record it if it is a definition, otherwise look inside it.

        Split out of `_visit` because a callable's *body* has to be asked this question too,
        and `_visit(body)` only ever asked it of the body's children. A body that is itself a
        definition was therefore skipped and only its contents were walked -- so
        `const add = (a) => (b) => a + b` produced one entity, and `lambda a: lambda b: a + b`
        likewise. Curried callables were invisible to every metric and every ceiling.
        """
        definition = self.profile.unwrap(child)
        if self.profile.is_definition(definition):
            self._record(child, definition, parent, prefix, inside_type)
        else:
            self._visit(child, parent, prefix, inside_type)

    def _record(
        self, child: Node, definition: Node, parent: Entity, prefix: str, inside_type: bool
    ) -> None:
        """Add one definition, then walk whatever it contains."""
        name = self.profile.entity_name(definition)
        kind = self._classify(definition, inside_type, name)
        local = name or f"<{definition.type}@{definition.start_point[0] + 1}>"
        qualified = f"{prefix}.{local}" if prefix else local

        # The wrapper carries the span so decorator lines belong to the entity; the
        # definition carries everything else.
        entity = self._add(
            _NewEntity(
                kind=kind,
                name=name,
                qualified_name=qualified,
                parent_id=parent.id,
                attrs=self._attrs(child, definition, kind),
            ),
            child,
        )
        self._descend(definition, entity, qualified, kind)

    def _attrs(self, child: Node, definition: Node, kind: EntityKind) -> dict[str, object]:
        """The entity spans the *wrapper* so decorator lines belong to it, but the
        definition's own range is what SCIP occurrences and metric visitors join against.
        Keeping both avoids every consumer re-deriving one from the other."""
        attrs: dict[str, object] = {
            "node_kind": definition.type,
            "def_range": [definition.start_byte, definition.end_byte],
            **_kind_attrs(definition, kind, self.profile),
        }
        if child is not definition:
            attrs["wrapped_by"] = child.type
        if _is_abstract(definition, self.profile):
            attrs["is_abstract"] = True
        return attrs

    def _descend(self, definition: Node, entity: Entity, qualified: str, kind: EntityKind) -> None:
        body = definition.child_by_field_name(self.profile.body_field)
        if body is not None:
            inside_type = kind in {EntityKind.CLASS, EntityKind.INTERFACE}
            self._consider(body, entity, qualified, inside_type)
            return
        # Arrow functions and lambdas have expression bodies, not a body field.
        for grandchild in definition.named_children:
            self._consider(grandchild, entity, qualified, inside_type=False)


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
