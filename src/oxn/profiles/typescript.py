"""TypeScript and JavaScript profiles.

Node kinds verified against the ``tree-sitter-typescript`` and ``tree-sitter-javascript``
grammars in ``tree-sitter-language-pack`` 1.15.8 on 2026-08-29.

Two findings worth stating, because both contradict the usual description:

* ``abstract_class_declaration`` is a **distinct node kind**, not ``class_declaration``
  carrying an ``abstract`` modifier. Abstractness (P4) reads the kind, not a modifier.
* an ``else if`` is an ``else_clause`` whose named child is an ``if_statement`` rather than
  a ``statement_block``. Relevant to cognitive complexity in P2.
"""

from __future__ import annotations

from oxn.profiles.base import LanguageProfile, Wrapper

_FUNCTION_LIKE = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "function_expression",
        "generator_function",
        "arrow_function",
        "method_definition",
        "method_signature",
        "abstract_method_signature",
    }
)

_CLASS_LIKE = frozenset(
    {
        "class_declaration",
        "abstract_class_declaration",
        "class",
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
    }
)

_WRAPPERS = {"export_statement": Wrapper("export_statement", "declaration")}

_PARAMETER_KINDS = frozenset({"required_parameter", "optional_parameter", "rest_pattern"})

TYPESCRIPT = LanguageProfile(
    name="typescript",
    grammar="typescript",
    extensions=frozenset({".ts", ".tsx", ".mts", ".cts"}),
    version=1,
    function_like=_FUNCTION_LIKE,
    class_like=_CLASS_LIKE,
    wrappers=_WRAPPERS,
    name_field="name",
    body_field="body",
    params_field="parameters",
    parameter_kinds=_PARAMETER_KINDS,
    receiver_kinds=frozenset(),
    comment_kinds=frozenset({"comment"}),
    string_kinds=frozenset({"string", "template_string"}),
    abstract_markers=frozenset({"abstract"}),
    abstract_kinds=frozenset(
        {"interface_declaration", "abstract_class_declaration", "type_alias_declaration"}
    ),
)

JAVASCRIPT = LanguageProfile(
    name="javascript",
    grammar="javascript",
    extensions=frozenset({".js", ".jsx", ".mjs", ".cjs"}),
    version=1,
    function_like=_FUNCTION_LIKE,
    # JS has no interfaces or type aliases; a class is the only type declaration.
    class_like=frozenset({"class_declaration", "class"}),
    wrappers=_WRAPPERS,
    name_field="name",
    body_field="body",
    params_field="parameters",
    parameter_kinds=frozenset(
        {"identifier", "rest_pattern", "assignment_pattern", "object_pattern", "array_pattern"}
    ),
    receiver_kinds=frozenset(),
    comment_kinds=frozenset({"comment"}),
    string_kinds=frozenset({"string", "template_string"}),
    abstract_markers=frozenset(),
    abstract_kinds=frozenset(),
)
