"""Rust language profile.

Node kinds verified against ``tree-sitter-rust`` in ``tree-sitter-language-pack`` 1.15.8.

* Rust is expression-oriented: ``if`` and ``match`` are ``*_expression`` nodes, and an
  ``if`` used as a value is the same node as one used as a statement.
* ``else if`` is wrapped in an ``else_clause``, the TypeScript shape rather than Go's.
* The ``?`` operator is a real branch -- it returns early on an error -- so it counts, which
  diverges from Lizard and is recorded in docs/divergences.md.
* A type's methods live in ``impl_item`` blocks that may be spread across files, so class
  membership needs L1 rather than syntax alone.
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

RUST_METRICS = MetricSpec(
    cyclomatic=CyclomaticSpec(
        decision_points=frozenset(
            {
                # `if let` is one branch: counting `let_condition` as well doubles it.
                "if_expression",
                "for_expression",
                "while_expression",
                "loop_expression",
                # `match` is handled as an exhaustive multiway construct, not per arm.
                # `?` propagates an error, which is a branch out of the function.
                "try_expression",
            }
        ),
        exhaustive_multiway_kinds=frozenset({"match_expression"}),
        multiway_branch_kinds=frozenset({"match_arm"}),
        boolean_node="binary_expression",
        boolean_operators=_BOOLEAN_OPS,
    ),
    cognitive=CognitiveSpec(
        structural=frozenset(
            {
                "if_expression",
                "for_expression",
                "while_expression",
                "loop_expression",
                "match_expression",
            }
        ),
        hybrid=frozenset({"else_clause"}),
        nesting_only=frozenset({"closure_expression", "function_item"}),
        fundamental=frozenset(),
        labelled_jump_kinds=frozenset({"break_expression", "continue_expression"}),
        jump_label_kinds=frozenset({"loop_label", "label"}),
        ignored=frozenset({"block"}),
        boolean_node="binary_expression",
        boolean_operators=_BOOLEAN_OPS,
        hybrid_parents=frozenset({"if_expression"}),
        alternative_style="wrapped",
        else_clause_kind="else_clause",
        if_kind="if_expression",
        call_kinds=frozenset({"call_expression", "macro_invocation"}),
        callee_field="function",
    ),
    size=SizeSpec(
        statement_kinds=frozenset(
            {
                "expression_statement",
                "let_declaration",
                "use_declaration",
                "function_item",
                "struct_item",
                "enum_item",
                "impl_item",
                "trait_item",
                "mod_item",
                "const_item",
                "static_item",
                "type_item",
                "macro_definition",
                "return_expression",
                "attribute_item",
                "extern_crate_declaration",
            }
        ),
        # `?` leaves the function on the error path. Go spells the same control flow
        # `if err != nil { return err }`, which is counted, so omitting it here made the two
        # languages incomparable on the most common error-handling shape either has.
        return_kinds=frozenset({"return_expression", "try_expression"}),
        docstrings_are_comments=False,
        tail_expression_returns=True,
    ),
    halstead=HalsteadSpec(
        spec_version=1,
        operand_kinds=frozenset(
            {
                "identifier",
                "type_identifier",
                "field_identifier",
                "primitive_type",
                "integer_literal",
                "float_literal",
                "string_literal",
                "char_literal",
                "boolean_literal",
                "raw_string_literal",
                "self",
                "crate",
                "super",
                "string_content",
                "shorthand_field_identifier",
            }
        ),
        excluded_tokens=frozenset({"\n", "", ";"}),
        excluded_kinds=frozenset({"line_comment", "block_comment"}),
    ),
    imports=ImportSpec(
        style="rust",
        statement_kinds=frozenset({"use_declaration", "extern_crate_declaration"}),
        module_field="argument",
        name_field="name",
        string_kinds=frozenset({"string_literal"}),
        call_kinds=frozenset({"call_expression"}),
    ),
    scopes=ScopeSpec(
        self_parameter_kind="self_parameter",
        style="rust",
        scope_kinds={
            "source_file": "module",
            "function_item": "function",
            "closure_expression": "lambda",
            "impl_item": "class",
            "trait_item": "class",
            "struct_item": "class",
            "mod_item": "module",
            "block": "block",
        },
        declaration_kinds=frozenset(
            {"function_item", "struct_item", "enum_item", "trait_item", "mod_item"}
        ),
        parameter_containers=frozenset({"parameters"}),
        assignment_kinds=frozenset({"let_declaration", "assignment_expression", "for_expression"}),
        alias_kinds=frozenset({"use_as_clause"}),
        rebinding_kinds=frozenset(),
        attribute_kind="field_expression",
        attribute_object_field="value",
        attribute_name_field="field",
        identifier_kind="identifier",
    ),
)

RUST = LanguageProfile(
    name="rust",
    grammar="rust",
    extensions=frozenset({".rs"}),
    version=1,
    function_like=frozenset({"function_item", "function_signature_item", "closure_expression"}),
    implements_field="type",
    supertype_fields=frozenset({"trait"}),
    class_like=frozenset({"struct_item", "enum_item", "trait_item", "union_item", "impl_item"}),
    wrappers={},
    name_field="name",
    body_field="body",
    params_field="parameters",
    parameter_kinds=frozenset({"parameter", "self_parameter", "variadic_parameter"}),
    # `&self` is a syntactic receiver, so it is excluded from the parameter list.
    receiver_kinds=frozenset({"self_parameter"}),
    comment_kinds=frozenset({"line_comment", "block_comment"}),
    string_kinds=frozenset({"string_literal", "raw_string_literal"}),
    abstract_markers=frozenset(),
    abstract_kinds=frozenset({"trait_item"}),
    metrics=RUST_METRICS,
)
