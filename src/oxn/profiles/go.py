"""Go language profile.

Node kinds verified against the ``tree-sitter-go`` grammar in
``tree-sitter-language-pack`` 1.15.8. Three facts shape this profile:

* Go has **no ``while``** -- ``for`` covers every loop, with an optional ``for_clause`` or
  ``range_clause``.
* Go has **no exceptions**, so there is no ``catch`` to increment for. ``select`` is the
  closest analogue to a multi-way branch and its ``communication_case``s each branch.
* ``else if`` sits **directly** in the ``alternative`` field -- there is no ``else`` node to
  match on, unlike Python's ``elif_clause`` or Rust's ``else_clause``.
"""

from __future__ import annotations

from oxn.profiles.base import LanguageProfile
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

GO_METRICS = MetricSpec(
    cyclomatic=CyclomaticSpec(
        decision_points=frozenset(
            {
                "if_statement",
                "for_statement",
                "expression_case",
                "type_case",
                "communication_case",
                # `x, ok := m[k]` and `v, err := f()` do not branch; only explicit cases do.
            }
        ),
        boolean_node="binary_expression",
        boolean_operators=_BOOLEAN_OPS,
    ),
    cognitive=CognitiveSpec(
        structural=frozenset(
            {
                "if_statement",
                "for_statement",
                "expression_switch_statement",
                "type_switch_statement",
                "select_statement",
            }
        ),
        hybrid=frozenset(),
        nesting_only=frozenset({"func_literal", "function_declaration", "method_declaration"}),
        fundamental=frozenset(),
        labelled_jump_kinds=frozenset({"break_statement", "continue_statement", "goto_statement"}),
        jump_label_kinds=frozenset({"label_name", "identifier"}),
        ignored=frozenset({"block"}),
        boolean_node="binary_expression",
        boolean_operators=_BOOLEAN_OPS,
        alternative_style="direct",
        plain_else_kinds=frozenset({"block"}),
        if_kind="if_statement",
        call_kinds=frozenset({"call_expression"}),
        callee_field="function",
    ),
    size=SizeSpec(
        statement_kinds=frozenset(
            {
                "expression_statement",
                "short_var_declaration",
                "var_declaration",
                "const_declaration",
                "assignment_statement",
                "return_statement",
                "if_statement",
                "for_statement",
                "expression_switch_statement",
                "type_switch_statement",
                "select_statement",
                "go_statement",
                "defer_statement",
                "type_declaration",
                "function_declaration",
                "method_declaration",
                "import_declaration",
                "package_clause",
                "inc_statement",
                "dec_statement",
                "send_statement",
                "labeled_statement",
            }
        ),
        return_kinds=frozenset({"return_statement"}),
        docstrings_are_comments=False,
    ),
    halstead=HalsteadSpec(
        spec_version=1,
        operand_kinds=frozenset(
            {
                "identifier",
                "type_identifier",
                "field_identifier",
                "package_identifier",
                "label_name",
                "int_literal",
                "float_literal",
                "imaginary_literal",
                "rune_literal",
                "interpreted_string_literal",
                "raw_string_literal",
                "true",
                "false",
                "nil",
                "iota",
            }
        ),
        excluded_tokens=frozenset({"\n", "", ";"}),
        excluded_kinds=frozenset({"comment"}),
    ),
    imports=ImportSpec(
        style="go",
        statement_kinds=frozenset({"import_declaration"}),
        module_field="path",
        name_field="name",
        string_kinds=frozenset({"interpreted_string_literal", "raw_string_literal"}),
        call_kinds=frozenset({"call_expression"}),
    ),
    scopes=ScopeSpec(
        style="go",
        scope_kinds={
            "source_file": "module",
            "function_declaration": "function",
            "method_declaration": "function",
            "func_literal": "lambda",
            "block": "block",
            "for_statement": "block",
            "if_statement": "block",
            "type_declaration": "class",
        },
        declaration_kinds=frozenset(
            {"function_declaration", "method_declaration", "type_declaration"}
        ),
        parameter_containers=frozenset({"parameter_list"}),
        assignment_kinds=frozenset(
            {"short_var_declaration", "var_spec", "assignment_statement", "range_clause"}
        ),
        alias_kinds=frozenset({"import_spec"}),
        rebinding_kinds=frozenset(),
        attribute_kind="selector_expression",
        attribute_object_field="operand",
        attribute_name_field="field",
        identifier_kind="identifier",
    ),
)

GO = LanguageProfile(
    name="go",
    grammar="go",
    extensions=frozenset({".go"}),
    version=2,
    privacy="casing",
    function_like=frozenset({"function_declaration", "method_declaration", "func_literal"}),
    # A Go "type" is a struct, an interface or a named alias; `type_declaration` wraps them.
    class_like=frozenset({"type_declaration", "type_spec"}),
    wrappers={},
    name_field="name",
    body_field="body",
    params_field="parameters",
    parameter_kinds=frozenset({"parameter_declaration", "variadic_parameter_declaration"}),
    # Go's receiver is syntactic and explicit, unlike Python's first parameter.
    receiver_kinds=frozenset(),
    comment_kinds=frozenset({"comment"}),
    string_kinds=frozenset({"interpreted_string_literal", "raw_string_literal"}),
    abstract_markers=frozenset(),
    abstract_kinds=frozenset({"interface_type"}),
    metrics=GO_METRICS,
)
