"""Extract import edges from a parse tree.

Two rules govern this module, and both come from ``docs/metrics.md`` section 4.1:

* **A full-tree walk, not a scan of top-level statements.** Imports hide inside
  ``if TYPE_CHECKING:`` blocks, function bodies and conditional branches. Missing them
  silently under-reports coupling, which is the failure mode that turns a layer gate into a
  false-negative machine.
* **Nothing is ever silently dropped.** An import OXN cannot resolve is recorded with
  ``resolved=False`` and its raw specifier, and surfaced in the CLI output. Dropping it
  would quietly hide a layer violation.

Every edge carries a ``kind``: a ``type_only`` import creates no runtime coupling and is
excluded from ``DEPENDS_ON`` by default, which is what Martin's metrics mean by a dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator

    from tree_sitter import Node

    from oxn.profiles.base import LanguageProfile
    from oxn.profiles.spec import ImportSpec


@dataclass(frozen=True, slots=True)
class RawImport:
    """One import statement, before module resolution."""

    #: The specifier exactly as written: ``a.b.c``, ``./mod``, ``../other``.
    specifier: str
    #: ``value`` | ``type_only`` | ``side_effect`` | ``wildcard`` | ``dynamic``
    kind: str
    line: int
    #: Leading-dot count for a Python relative import; 0 for absolute.
    level: int = 0
    #: Names imported *from* the module, where the language distinguishes them.
    names: tuple[str, ...] = ()

    @property
    def is_relative(self) -> bool:
        return self.level > 0 or self.specifier.startswith(".")


def extract_imports(root: Node, profile: LanguageProfile) -> list[RawImport]:
    """Every import in a file, in source order."""
    spec = profile.metrics.imports
    if not spec.statement_kinds and not spec.call_kinds:
        return []

    found = [
        item for node, type_only in _walk(root, spec) for item in _imports_at(node, spec, type_only)
    ]
    found.sort(key=lambda item: (item.line, item.specifier))
    return found


def _walk(root: Node, spec: ImportSpec) -> Iterator[tuple[Node, bool]]:
    """Every node, paired with whether it sits inside a type-only guard."""
    stack: list[tuple[Node, bool]] = [(root, False)]
    while stack:
        node, guarded = stack.pop()
        yield node, guarded
        inner = guarded or _is_type_checking_guard(node, spec)
        stack.extend((child, inner) for child in node.named_children)


def _imports_at(node: Node, spec: ImportSpec, type_only: bool) -> list[RawImport]:
    """What this one node imports, if anything."""
    if node.type in spec.statement_kinds:
        statements = _from_statement(node, spec)
        return [replace(item, kind="type_only") for item in statements] if type_only else statements
    if node.type in spec.call_kinds:
        dynamic = _from_dynamic_call(node, spec)
        return [dynamic] if dynamic is not None else []
    return []


def _is_type_checking_guard(node: Node, spec: ImportSpec) -> bool:
    """Is this an `if TYPE_CHECKING:` block?

    Python has no `import type` keyword, so its type-only imports are ordinary imports
    nested inside this guard -- erased at runtime exactly as TypeScript's are. Counting them
    as runtime coupling inflates every Martin metric, can manufacture a cycle out of nothing,
    and produced two false layer-contract violations in OXN's own repository, which is where
    this was found.

    The condition is matched on the name rather than on a resolved symbol: `TYPE_CHECKING`
    is `typing.TYPE_CHECKING` by universal convention, and a name that says it and means
    something else is a problem no import graph can help with.
    """
    if spec.style != "python" or node.type != "if_statement":
        return False
    condition = node.child_by_field_name("condition")
    return condition is not None and b"TYPE_CHECKING" in (condition.text or b"")


# ---- statements -----------------------------------------------------------------------


def _from_statement(node: Node, spec: ImportSpec) -> list[RawImport]:
    handler = {
        "python": _python_statement,
        "ecmascript": _ecmascript_statement,
        "go": _go_statement,
        "rust": _rust_statement,
        "java": _java_statement,
    }.get(spec.style)
    return handler(node, spec) if handler else []


def _go_statement(node: Node, spec: ImportSpec) -> list[RawImport]:
    """``import ("fmt"; alias "os")`` -- one declaration, any number of specs.

    A ``_`` alias imports purely for side effects and a ``.`` alias dot-imports; both are
    real coupling and are recorded with the kind that says which.
    """
    line = node.start_point[0] + 1
    found: list[RawImport] = []
    for spec_node in _descend(node, "import_spec"):
        path = spec_node.child_by_field_name("path")
        if path is None:
            continue
        specifier = _strip_quotes(_text(path))
        if not specifier:
            continue
        alias = spec_node.child_by_field_name("name")
        alias_text = _text(alias) if alias is not None else ""
        kind = "side_effect" if alias_text == "_" else "value"
        found.append(RawImport(specifier, kind, spec_node.start_point[0] + 1 or line))
    return found


def _rust_statement(node: Node, spec: ImportSpec) -> list[RawImport]:
    """``use a::b::{c, d as e};`` -- one declaration reaching several paths.

    Rust paths are ``::``-separated, and are normalised to dots so every language's
    specifiers read the same way downstream.
    """
    line = node.start_point[0] + 1
    if node.type == "extern_crate_declaration":
        name = node.child_by_field_name("name")
        return [RawImport(_text(name), "value", line)] if name is not None else []

    argument = node.child_by_field_name("argument")
    if argument is None:
        return []
    return [
        RawImport(path.replace("::", "."), kind, line)
        for path, kind in _rust_paths(argument, prefix="")
    ]


def _rust_paths(node: Node, prefix: str) -> list[tuple[str, str]]:
    """Flatten a ``use`` tree into ``(path, kind)`` pairs."""
    if node.type == "scoped_use_list":
        base = node.child_by_field_name("path")
        head = f"{prefix}::{_text(base)}" if prefix and base is not None else _text(base or node)
        body = node.child_by_field_name("list")
        if body is None:
            return [(head, "value")]
        out: list[tuple[str, str]] = []
        for child in body.named_children:
            out.extend(_rust_paths(child, head))
        return out
    if node.type == "use_list":
        out = []
        for child in node.named_children:
            out.extend(_rust_paths(child, prefix))
        return out
    if node.type == "use_as_clause":
        path = node.child_by_field_name("path")
        return _rust_paths(path, prefix) if path is not None else []
    if node.type == "use_wildcard":
        return [(prefix or _text(node), "wildcard")]

    text = _text(node)
    if not text:
        return []
    return [(f"{prefix}::{text}" if prefix else text, "value")]


def _java_statement(node: Node, spec: ImportSpec) -> list[RawImport]:
    """``import java.util.List;`` and ``import static java.lang.Math.max;``.

    A wildcard import ends in ``.*``; a static import names a member rather than a type, and
    both couple to the same package either way.
    """
    line = node.start_point[0] + 1
    wildcard = any(not child.is_named and _text(child) == "*" for child in node.children)
    static = any(not child.is_named and _text(child) == "static" for child in node.children)

    for child in node.named_children:
        if child.type in {"scoped_identifier", "identifier"}:
            specifier = _text(child)
            if not specifier:
                continue
            kind = "wildcard" if wildcard else "value"
            names: tuple[str, ...] = ()
            if static:
                # `import static a.b.C.member` -- the type is the specifier's parent.
                specifier, _, member = specifier.rpartition(".")
                names = (member,)
            return [RawImport(specifier, kind, line, names=names)]
    return []


def _descend(node: Node, kind: str) -> list[Node]:
    """Every descendant of a given kind, including the node itself."""
    found: list[Node] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == kind:
            found.append(current)
        stack.extend(current.named_children)
    return found


def _python_statement(node: Node, spec: ImportSpec) -> list[RawImport]:
    line = node.start_point[0] + 1

    if node.type == "import_statement":
        # `import a.b.c` and `import a.b as c` -- possibly several per statement.
        return [
            RawImport(specifier=specifier, kind="value", line=line)
            for specifier in _module_names(node, spec)
        ]

    module = node.child_by_field_name(spec.module_field)
    if module is None:
        return []

    level, specifier = _python_module_ref(module, spec)
    # `from x import y as z` -- resolution needs `y`, the real submodule name, not `z`.
    names = tuple(
        _unaliased(child, spec)
        for child in node.children_by_field_name(spec.name_field)
        if child.type not in spec.wildcard_kinds
    )
    wildcard = any(child.type in spec.wildcard_kinds for child in node.named_children)

    return [
        RawImport(
            specifier=specifier,
            kind="wildcard" if wildcard else "value",
            line=line,
            level=level,
            names=names,
        )
    ]


def _unaliased(node: Node, spec: ImportSpec) -> str:
    """The imported name with any ``as`` alias stripped."""
    if node.type in spec.alias_kinds:
        inner = node.child_by_field_name("name")
        if inner is not None:
            return _text(inner)
    return _text(node)


def _module_names(node: Node, spec: ImportSpec) -> list[str]:
    """Module paths named by an ``import a, b as c`` statement."""
    names: list[str] = []
    for child in node.children_by_field_name(spec.name_field):
        target = child
        if child.type in spec.alias_kinds:
            inner = child.child_by_field_name("name")
            if inner is not None:
                target = inner
        names.append(_text(target))
    return [name for name in names if name]


def _python_module_ref(module: Node, spec: ImportSpec) -> tuple[int, str]:
    """``(level, dotted path)`` for a ``from`` clause.

    ``from . import x`` is level 1 with an empty path; ``from ..pkg import x`` is level 2
    with path ``pkg``. The level is what anchors resolution to the importing file's package.
    """
    if module.type not in spec.relative_kinds:
        return 0, _text(module)

    level = 0
    remainder = ""
    for child in module.children:
        text = _text(child)
        if child.type == "import_prefix" or set(text) == {"."}:
            level += len(text)
        elif text:
            remainder = text
    return level, remainder


def _ecmascript_statement(node: Node, spec: ImportSpec) -> list[RawImport]:
    source = node.child_by_field_name(spec.module_field)
    if source is None:
        return []  # `export const x = 1` -- a declaration, not a dependency

    specifier = _strip_quotes(_text(source))
    if not specifier:
        return []

    line = node.start_point[0] + 1
    if spec.type_only_token and _has_token(node, spec.type_only_token):
        return [RawImport(specifier, "type_only", line)]

    # `import "./x"` with no clause runs the module for its side effects only.
    has_clause = any(
        child.type in {"import_clause", "export_clause", "namespace_import"}
        for child in node.named_children
    )
    kind = "value" if has_clause or node.type == "export_statement" else "side_effect"
    return [RawImport(specifier, kind, line)]


# ---- dynamic imports -------------------------------------------------------------------


def _from_dynamic_call(node: Node, spec: ImportSpec) -> RawImport | None:
    """``importlib.import_module("x")``, ``require("x")``, ``import("x")``.

    A literal argument is recorded as a real dependency; a computed one is recorded as
    unresolvable rather than discarded, because a dynamic import is still coupling.
    """
    callee = node.child_by_field_name("function")
    if callee is None or _text(callee) not in spec.dynamic_callees:
        return None

    arguments = node.child_by_field_name("arguments")
    line = node.start_point[0] + 1
    if arguments is None:
        return RawImport("", "dynamic", line)

    for argument in arguments.named_children:
        if argument.type in spec.string_kinds:
            return RawImport(_strip_quotes(_text(argument)), "dynamic", line)
    return RawImport("", "dynamic", line)


# ---- helpers ---------------------------------------------------------------------------


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace") if node.text else ""


def _strip_quotes(raw: str) -> str:
    return raw.strip().strip("\"'`")


def _has_token(node: Node, token: str) -> bool:
    """True if an anonymous child is exactly ``token`` -- e.g. the ``type`` in ``import type``."""
    return any(not child.is_named and _text(child) == token for child in node.children)
