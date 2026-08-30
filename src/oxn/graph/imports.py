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

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
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

    found: list[RawImport] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in spec.statement_kinds:
            found.extend(_from_statement(node, spec))
        elif node.type in spec.call_kinds:
            dynamic = _from_dynamic_call(node, spec)
            if dynamic is not None:
                found.append(dynamic)
        stack.extend(node.named_children)

    found.sort(key=lambda item: (item.line, item.specifier))
    return found


# ---- statements -----------------------------------------------------------------------


def _from_statement(node: Node, spec: ImportSpec) -> list[RawImport]:
    if spec.style == "python":
        return _python_statement(node, spec)
    return _ecmascript_statement(node, spec)


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
