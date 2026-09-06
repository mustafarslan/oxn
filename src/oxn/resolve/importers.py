"""How Go, Rust and Java spell an import.

Three grammars that are their own shape rather than a variant of Python's or ECMAScript's,
and the one thing they are all asked for: **the local name a file may write, and where it
came from**. `scopes.py` calls this the alias table, which is a Python and TypeScript name
for it -- Java has no import aliases at all, and the table is still exactly what a resolver
needs from Java.

**These recorded nothing until 2026-09-06.** `_imported_names` dispatched on
`style != "python"` straight into the ECMAScript reader, which finds no ECMAScript import
clause inside a `use_declaration` and yields nothing at all. Go's scope spec even declared
`alias_kinds={"import_spec"}`, so the intent was written down and nothing acted on it.

Separated from `scopes.py` when that file crossed its 500-line ceiling. The split is along a
real seam: everything here is a statement about one language's import syntax, and everything
there is about scopes and bindings in general.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable, Iterator

    from tree_sitter import Node


#: Go's semantic import versioning: a trailing `/v2` segment is a *module* major version and
#: never the package name -- `github.com/casbin/casbin/v2` is imported as `casbin`. gopkg.in
#: spells the same thing with a dot: `gopkg.in/yaml.v2` is `yaml`. Both are documented
#: conventions rather than guesses, and go-kit imports six paths of the first shape.
_GO_MAJOR = re.compile(r"^v[0-9]+$")
_GO_DOTTED_MAJOR = re.compile(r"^(?P<name>.+)\.v[0-9]+$")


def _go_package_name(path: str) -> str:
    """The local name an unaliased Go import binds.

    **This is Go's convention, not Go's rule.** The real name comes from the target's
    `package` clause, which OXN has not read; the last path segment is what it almost always
    is. Where they disagree the segment is usually not a valid identifier at all --
    `opentracing-go`, `nats.go`, both in go-kit -- so the entry is *inert* rather than wrong:
    no call qualifier can ever equal it. A target whose clause disagrees while still being a
    valid identifier is mis-bound here, and syntax cannot tell.
    """
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) > 1 and _GO_MAJOR.match(segments[-1]):
        segments.pop()
    tail = segments[-1] if segments else ""
    dotted = _GO_DOTTED_MAJOR.match(tail)
    return dotted.group("name") if dotted else tail


def _go_import_specs(node: Node) -> Iterator[Node]:
    """Every `import_spec`, whether written alone or inside a parenthesised group."""
    for child in node.named_children:
        if child.type == "import_spec":
            yield child
        elif child.type == "import_spec_list":
            yield from (spec for spec in child.named_children if spec.type == "import_spec")


def _go_imported_names(node: Node) -> Iterator[tuple[str, str]]:
    """``(name, source)`` for each package a Go import binds.

    `import ex "e.com/t"` binds `ex`; unaliased, the name is the path's own (see
    `_go_package_name`). `_ "x"` binds nothing -- it is imported for its side effects -- and
    `. "x"` binds nothing *here*: a dot import puts the package's members into file scope
    with no qualifier at all, which is a different table from this one.
    """
    for spec_node in _go_import_specs(node):
        path_node = spec_node.child_by_field_name("path")
        path = _strip_quotes(_text(path_node)) if path_node is not None else ""
        if not path:
            continue
        alias = spec_node.child_by_field_name("name")
        if alias is None:
            name = _go_package_name(path)
        elif alias.type == "package_identifier":
            name = _text(alias)
        else:
            continue
        if name:
            yield name, path


def _rust_imported_names(node: Node) -> Iterator[tuple[str, str]]:
    """``(name, source)`` for each name a Rust `use` or `extern crate` binds."""
    if node.type == "extern_crate_declaration":
        alias = node.child_by_field_name("alias") or node.child_by_field_name("name")
        source = node.child_by_field_name("name")
        if alias is not None and source is not None:
            yield _text(alias), _text(source)
        return
    yield from _rust_use_names(node.child_by_field_name("argument"), "")


def _rust_use_names(node: Node | None, prefix: str) -> Iterator[tuple[str, str]]:
    """Walk a `use` tree, carrying the module path accumulated so far.

    `use x::y::{p, q as r}` binds `p` and `r`, both sourced to `x::y`, and the lists nest
    (`use a::{b::{c, d}}`) so this recurses. `use s::t::*` binds no name syntax can name.

    `use_list` and `scoped_use_list` share a branch because they are the same operation with
    and without a prefix: an absent `path` field joins to nothing, and an absent `list` field
    means the node *is* the list.
    """
    if node is None:
        return
    if node.type in {"identifier", "self"}:
        # `self` is its own node kind in the grammar, not an identifier: checked, because
        # treating it as one silently drops the `use a::{self, b}` binding.
        yield from _rust_leaf(_text(node), prefix)
    elif node.type == "scoped_identifier":
        inner = _rust_join(prefix, _text(node.child_by_field_name("path")))
        yield from _rust_leaf(_text(node.child_by_field_name("name")), inner)
    elif node.type == "use_as_clause":
        yield from _rust_alias(node, prefix)
    elif node.type in {"use_list", "scoped_use_list"}:
        listed = node.child_by_field_name("list") or node
        inner = _rust_join(prefix, _text(node.child_by_field_name("path")))
        for child in listed.named_children:
            yield from _rust_use_names(child, inner)


def _rust_alias(node: Node, prefix: str) -> Iterator[tuple[str, str]]:
    """`a::b as c` binds `c`, sourced to wherever `b` lives.

    Its own function because it is its own rule, not because the caller was long: the target
    is either a scoped path, whose `path` field names the source, or a bare name inside a
    list -- `use x::y::{q as r}` -- where the source is the prefix already accumulated.
    """
    alias = _text(node.child_by_field_name("alias"))
    if not alias:
        return
    target = node.child_by_field_name("path")
    inner = _text(target.child_by_field_name("path")) if target is not None else ""
    yield alias, _rust_join(prefix, inner)


def _rust_leaf(name: str, source: str) -> Iterator[tuple[str, str]]:
    """One bound name. `self` in a list re-binds the module itself: `use a::{self, b}`."""
    if name == "self":
        if source:
            yield source.rsplit("::", 1)[-1], source
    elif name:
        yield name, source


def _rust_join(prefix: str, path: str) -> str:
    if prefix and path:
        return f"{prefix}::{path}"
    return path or prefix


def _java_imported_names(node: Node) -> Iterator[tuple[str, str]]:
    """``(name, source)`` for a Java import.

    **Java has no import aliases**, which is why this table is not named after them: it maps
    a simple name to where it came from, and `import java.util.List` is exactly that. An
    on-demand import (`java.util.*`) enumerates nothing, so it binds nothing. A static
    import looks identical at this level -- `Math.max` binds `max` and is sourced to
    `java.lang.Math` -- and that is the right answer for both.
    """
    if any(child.type == "asterisk" for child in node.named_children):
        return
    for child in node.named_children:
        if child.type != "scoped_identifier":
            continue
        name = _text(child.child_by_field_name("name"))
        if name:
            yield name, _text(child.child_by_field_name("scope"))


#: Import shapes that are their own grammar rather than a variant of Python's or
#: ECMAScript's. Data, not a branch: the alternative is an `if spec.style ==` chain that
#: every new language has to be threaded through.
IMPORT_READERS: dict[str, Callable[[Node], Iterator[tuple[str, str]]]] = {
    "go": _go_imported_names,
    "rust": _rust_imported_names,
    "java": _java_imported_names,
}


def _text(node: Node | None) -> str:
    """The source text of a node, and ``""`` for a field that is not present.

    Private here rather than shared with `scopes.py`, which has the same two lines: a module
    reaching into another for this would make `scopes` and `importers` import each other,
    and a cycle inside one layer is exactly what `oxn check --deep` exists to reject. Every
    tree-walking module in this repository carries its own.
    """
    if node is None:
        return ""
    return node.text.decode("utf-8", "replace") if node.text else ""


def _strip_quotes(raw: str) -> str:
    return raw.strip().strip("\"'`")
