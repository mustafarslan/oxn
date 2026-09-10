"""Java language profile.

Node kinds verified against ``tree-sitter-java`` in ``tree-sitter-language-pack`` 1.15.8.

* ``else if`` sits **directly** in the ``alternative`` field, the Go shape.
* A ``switch`` and all its ``case``s together incur one cognitive increment, while each
  ``case`` is a separate cyclomatic decision point -- the clearest illustration of why the
  two metrics disagree, and precisely the example the Cognitive Complexity white paper opens
  with.
* ``this.field`` is a ``field_access`` node, so cohesion metrics have a syntactic receiver
  rather than Python's positional one.
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

JAVA_METRICS = MetricSpec(
    cyclomatic=CyclomaticSpec(
        decision_points=frozenset(
            {
                "if_statement",
                "ternary_expression",
                "for_statement",
                "enhanced_for_statement",
                "while_statement",
                "do_statement",
                "catch_clause",
                "switch_label",
            }
        ),
        # `default:` is a `switch_label` with no named child, exactly like `case _` in
        # Python and `_ =>` in Rust: the fall-through, not a branch of its own.
        catch_all_kinds=frozenset({"switch_label"}),
        catch_all_pattern_kinds=frozenset({"switch_label"}),
        boolean_node="binary_expression",
        boolean_operators=_BOOLEAN_OPS,
    ),
    cognitive=CognitiveSpec(
        structural=frozenset(
            {
                "if_statement",
                "ternary_expression",
                "for_statement",
                "enhanced_for_statement",
                "while_statement",
                "do_statement",
                "catch_clause",
                "switch_expression",
                "switch_statement",
            }
        ),
        hybrid=frozenset(),
        nesting_only=frozenset({"lambda_expression", "method_declaration", "class_declaration"}),
        fundamental=frozenset(),
        labelled_jump_kinds=frozenset({"break_statement", "continue_statement"}),
        jump_label_kinds=frozenset({"identifier"}),
        ignored=frozenset({"try_statement", "finally_clause", "block"}),
        boolean_node="binary_expression",
        boolean_operators=_BOOLEAN_OPS,
        alternative_style="direct",
        plain_else_kinds=frozenset({"block", "expression_statement"}),
        if_kind="if_statement",
        call_kinds=frozenset({"method_invocation", "object_creation_expression"}),
        callee_field="name",
    ),
    size=SizeSpec(
        statement_kinds=frozenset(
            {
                "expression_statement",
                "local_variable_declaration",
                "return_statement",
                "if_statement",
                "for_statement",
                "enhanced_for_statement",
                "while_statement",
                "do_statement",
                "try_statement",
                "switch_expression",
                "throw_statement",
                "break_statement",
                "continue_statement",
                "import_declaration",
                "package_declaration",
                "class_declaration",
                "interface_declaration",
                "method_declaration",
                "field_declaration",
                "constructor_declaration",
                "enum_declaration",
                "assert_statement",
            }
        ),
        return_kinds=frozenset({"return_statement", "throw_statement"}),
        docstrings_are_comments=False,
    ),
    halstead=HalsteadSpec(
        spec_version=1,
        operand_kinds=frozenset(
            {
                "identifier",
                "type_identifier",
                "field_access",
                "scoped_identifier",
                "decimal_integer_literal",
                "decimal_floating_point_literal",
                "string_literal",
                "character_literal",
                "true",
                "false",
                "null_literal",
                "this",
                "super",
                "void_type",
                "integral_type",
                "floating_point_type",
                "boolean_type",
            }
        ),
        excluded_tokens=frozenset({"\n", "", ";"}),
        excluded_kinds=frozenset({"line_comment", "block_comment"}),
    ),
    imports=ImportSpec(
        style="java",
        statement_kinds=frozenset({"import_declaration"}),
        module_field="",
        name_field="name",
        string_kinds=frozenset({"string_literal"}),
        call_kinds=frozenset({"method_invocation"}),
    ),
    scopes=ScopeSpec(
        implicit_receiver="this",
        bare_field_access=True,
        style="java",
        scope_kinds={
            "program": "module",
            "class_declaration": "class",
            "interface_declaration": "class",
            "enum_declaration": "class",
            "record_declaration": "class",
            "method_declaration": "function",
            "constructor_declaration": "function",
            "lambda_expression": "lambda",
            "block": "block",
            "catch_clause": "block",
            "for_statement": "block",
        },
        declaration_kinds=frozenset(
            {
                "class_declaration",
                "interface_declaration",
                "method_declaration",
                "constructor_declaration",
                "enum_declaration",
                "record_declaration",
            }
        ),
        parameter_containers=frozenset({"formal_parameters"}),
        assignment_kinds=frozenset(
            {"local_variable_declaration", "variable_declarator", "assignment_expression"}
        ),
        alias_kinds=frozenset(),
        rebinding_kinds=frozenset(),
        attribute_kind="field_access",
        attribute_object_field="object",
        attribute_name_field="field",
        identifier_kind="identifier",
    ),
)

JAVA = LanguageProfile(
    name="java",
    grammar="java",
    extensions=frozenset({".java"}),
    # `private` on the method itself. Sound: it is the language saying so.
    privacy_rules=("modifier",),
    private_marker="modifiers",
    dispatch="class_name",
    version=1,
    function_like=frozenset({"method_declaration", "constructor_declaration", "lambda_expression"}),
    supertype_fields=frozenset({"superclass", "interfaces"}),
    class_like=frozenset(
        {
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
            "annotation_type_declaration",
        }
    ),
    wrappers={},
    name_field="name",
    body_field="body",
    params_field="parameters",
    parameter_kinds=frozenset({"formal_parameter", "spread_parameter"}),
    receiver_kinds=frozenset(),
    comment_kinds=frozenset({"line_comment", "block_comment"}),
    string_kinds=frozenset({"string_literal", "text_block"}),
    abstract_markers=frozenset({"abstract"}),
    abstract_kinds=frozenset({"interface_declaration", "annotation_type_declaration"}),
    metrics=JAVA_METRICS,
)
