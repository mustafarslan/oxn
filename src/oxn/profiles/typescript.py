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

from dataclasses import replace

from oxn.profiles.base import LanguageProfile, Wrapper
from oxn.profiles.spec import (
    CognitiveSpec,
    CyclomaticSpec,
    HalsteadSpec,
    ImportSpec,
    MetricSpec,
    ScopeSpec,
    SizeSpec,
)

_BOOLEAN_OPS = frozenset({"&&", "||"})

# Shared by TypeScript and JavaScript: the grammars agree on control flow.
_TS_METRICS = MetricSpec(
    cyclomatic=CyclomaticSpec(
        decision_points=frozenset(
            {
                "if_statement",
                "ternary_expression",
                "for_statement",
                "for_in_statement",
                "while_statement",
                "do_statement",
                "catch_clause",
                # Each `case` branches; `default` does not.
                "switch_case",
            }
        ),
        boolean_node="binary_expression",
        boolean_operators=_BOOLEAN_OPS,
    ),
    cognitive=CognitiveSpec(
        structural=frozenset(
            {
                "if_statement",
                "ternary_expression",
                "for_statement",
                "for_in_statement",
                "while_statement",
                "do_statement",
                "catch_clause",
                # A switch and all its cases together incur ONE increment.
                "switch_statement",
            }
        ),
        hybrid=frozenset({"else_clause"}),
        nesting_only=frozenset(
            {
                "function_declaration",
                "generator_function_declaration",
                "function_expression",
                "arrow_function",
                "method_definition",
            }
        ),
        fundamental=frozenset(),
        labelled_jump_kinds=frozenset({"break_statement", "continue_statement"}),
        # `try` and `finally` are ignored by the specification; `catch` is structural.
        ignored=frozenset({"try_statement", "finally_clause", "statement_block"}),
        boolean_node="binary_expression",
        boolean_operators=_BOOLEAN_OPS,
        hybrid_parents=frozenset({"if_statement"}),
        # `else if` is an else_clause wrapping an if_statement, verified against the grammar.
        alternative_style="wrapped",
        else_if_via_else_clause=True,
        else_clause_kind="else_clause",
        if_kind="if_statement",
        js_declarative_function_exception=True,
        call_kinds=frozenset({"call_expression"}),
        callee_field="function",
    ),
    size=SizeSpec(
        statement_kinds=frozenset(
            {
                "expression_statement",
                "variable_declaration",
                "lexical_declaration",
                "return_statement",
                "if_statement",
                "for_statement",
                "for_in_statement",
                "while_statement",
                "do_statement",
                "try_statement",
                "switch_statement",
                "throw_statement",
                "break_statement",
                "continue_statement",
                "import_statement",
                "export_statement",
                "class_declaration",
                "function_declaration",
                "interface_declaration",
                "type_alias_declaration",
                "enum_declaration",
            }
        ),
        return_kinds=frozenset({"return_statement", "throw_statement"}),
        statement_containers=frozenset({"statement_block", "program", "class_body"}),
        docstrings_are_comments=False,
    ),
    halstead=HalsteadSpec(
        spec_version=1,
        operand_kinds=frozenset(
            {
                "identifier",
                "type_identifier",
                "property_identifier",
                "private_property_identifier",
                "field_identifier",
                "statement_identifier",
                "integer",
                "float",
                "number",
                "string",
                "string_content",
                "string_fragment",
                "true",
                "false",
                "none",
                "null",
                "undefined",
                "ellipsis",
                "shorthand_property_identifier",
                "predefined_type",
            }
        ),
        excluded_tokens=frozenset({"\n", "", ":"}),
        excluded_kinds=frozenset({"comment"}),
    ),
    imports=ImportSpec(
        style="ecmascript",
        statement_kinds=frozenset({"import_statement", "export_statement"}),
        module_field="source",
        string_kinds=frozenset({"string"}),
        # `import type { T } from "x"` creates no runtime coupling, so it is excluded from
        # DEPENDS_ON by default -- which is what Martin's metrics are about.
        type_only_token="type",
        dynamic_callees=frozenset({"require", "import"}),
        reexport_targets=frozenset({"module.exports", "exports"}),
        call_kinds=frozenset({"call_expression"}),
    ),
    scopes=ScopeSpec(
        implicit_receiver="this",
        style="ecmascript",
        scope_kinds={
            "program": "module",
            "class_declaration": "class",
            "abstract_class_declaration": "class",
            "class_body": "class",
            "function_declaration": "function",
            "generator_function_declaration": "function",
            "function_expression": "function",
            "method_definition": "function",
            "arrow_function": "lambda",
            "statement_block": "block",
            "for_statement": "block",
            "for_in_statement": "block",
            "catch_clause": "block",
        },
        declaration_kinds=frozenset(
            {
                "function_declaration",
                "generator_function_declaration",
                "class_declaration",
                "abstract_class_declaration",
            }
        ),
        parameter_containers=frozenset({"formal_parameters"}),
        assignment_kinds=frozenset(
            {"variable_declarator", "assignment_expression", "for_in_statement"}
        ),
        binding_identifier_kinds=frozenset({"shorthand_property_identifier_pattern"}),
        alias_kinds=frozenset({"import_specifier", "namespace_import", "catch_clause"}),
        rebinding_kinds=frozenset(),
        attribute_kind="member_expression",
        attribute_object_field="object",
        attribute_name_field="property",
        identifier_kind="identifier",
    ),
)

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
    extensions=frozenset({".ts", ".mts", ".cts"}),
    # Three ways, any of which is sound: `#name`, the `private` modifier, and a top-level
    # declaration an ES module does not export.
    privacy="hash",
    dispatch="constructor",
    privacy_rules=("name", "modifier", "unexported"),
    private_marker="accessibility_modifier",
    export_wrapper="export_statement",
    version=2,
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
    metrics=_TS_METRICS,
)

#: The same language, a different grammar. `tree-sitter-typescript` ships `typescript` and
#: `tsx` separately and the first cannot parse JSX, so every `.tsx` file in a React project
#: came back `has_error` -- and every pass drops such a file without a word.
#:
#: Built with `replace` rather than written out, so the node-kind tables cannot drift: a
#: profile *is* a grammar's vocabulary, and two hand-maintained copies of it would agree
#: until the day one was edited. `name` stays "typescript" deliberately -- it is the label
#: every metric, cache row and report groups by, and a phantom "tsx" language would split
#: TypeScript's figures in half. Only `grammar` and `extensions` differ, which is what
#: `tests/test_grammars.py` asserts.
TSX = replace(TYPESCRIPT, grammar="tsx", extensions=frozenset({".tsx"}))

JAVASCRIPT = LanguageProfile(
    name="javascript",
    grammar="javascript",
    extensions=frozenset({".js", ".jsx", ".mjs", ".cjs"}),
    # `#name` is privacy the grammar guarantees; `private` does not exist here. The module
    # rule applies only to a file that uses ES module syntax -- CommonJS can publish anything
    # through `module.exports.x = x`, so the rule declines there rather than guessing.
    privacy="hash",
    dispatch="constructor",
    privacy_rules=("name", "unexported"),
    export_wrapper="export_statement",
    version=2,
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
    metrics=_TS_METRICS,
)
