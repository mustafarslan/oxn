"""Swift language profile (DX-X2).

Node kinds verified against the ``tree-sitter-swift`` grammar in ``tree-sitter-language-pack``
by parsing a real Swift code base (400 files of Aero, an app and its Swift package). Five facts
shape this profile:

* Swift spells ``else if`` with **no field at all**: the ``else`` token is followed by a sibling,
  the next ``if_statement`` or the plain ``else``'s ``statements``. That is the ``"keyword"``
  alternative style (`oxn.metrics.cognitive.alternatives_of`).
* ``&&`` and ``||`` are their own node kinds, ``conjunction_expression`` and
  ``disjunction_expression``, with ``lhs``, ``op`` and ``rhs`` fields, not one binary node. The
  cognitive engine reads them through ``boolean_kinds``, so a run of like operators is one
  increment as everywhere else; each operator is one cyclomatic decision point.
* ``struct``, ``class``, ``enum``, ``actor`` and ``extension`` all parse as ``class_declaration``;
  protocols are ``protocol_declaration``.
* ``guard ... else { }`` is a conditional with its own increment; its ``else`` is the only branch.
* Imports name a *module* (``import AeroCore``), never a file: a module is every Swift file of one
  SwiftPM target (see `oxn.graph.resolve._resolve_swift`).
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

_LOOPS = frozenset({"for_statement", "while_statement", "repeat_while_statement"})
_BOOLEANS = frozenset({"conjunction_expression", "disjunction_expression"})

SWIFT_METRICS = MetricSpec(
    cyclomatic=CyclomaticSpec(
        decision_points=frozenset(
            {"if_statement", "guard_statement", "switch_entry", "catch_block", "ternary_expression"}
            | _LOOPS
            | _BOOLEANS
        ),
    ),
    cognitive=CognitiveSpec(
        structural=frozenset(
            {
                "if_statement",
                "guard_statement",
                "switch_statement",
                "catch_block",
                "ternary_expression",
            }
            | _LOOPS
        ),
        hybrid=frozenset(),
        nesting_only=frozenset({"function_declaration", "lambda_literal", "init_declaration"}),
        fundamental=frozenset(),
        boolean_kinds=_BOOLEANS,
        boolean_operators=frozenset({"&&", "||"}),
        operator_field="op",
        side_fields=("lhs", "rhs"),
        labelled_jump_kinds=frozenset(),
        ignored=frozenset({"statements"}),
        alternative_style="keyword",
        plain_else_kinds=frozenset({"statements"}),
        if_kind="if_statement",
        call_kinds=frozenset(),
    ),
    size=SizeSpec(
        statement_kinds=frozenset(
            {
                "property_declaration",
                "control_transfer_statement",
                "if_statement",
                "guard_statement",
                "switch_statement",
                "do_statement",
                "call_expression",
                "assignment",
                "import_declaration",
                "function_declaration",
                "class_declaration",
                "protocol_declaration",
                "typealias_declaration",
            }
            | _LOOPS
        ),
        return_kinds=frozenset({"control_transfer_statement"}),
        statement_containers=frozenset({"statements", "source_file"}),
        docstrings_are_comments=True,
    ),
    halstead=HalsteadSpec(
        spec_version=1,
        operand_kinds=frozenset(
            {
                "simple_identifier",
                "type_identifier",
                "integer_literal",
                "real_literal",
                "hex_literal",
                "oct_literal",
                "bin_literal",
                "boolean_literal",
                "nil_literal",
                "line_string_literal",
                "multi_line_string_literal",
                "raw_string_literal",
            }
        ),
        excluded_tokens=frozenset({"\n", "", ";"}),
        excluded_kinds=frozenset({"comment", "multiline_comment"}),
    ),
    imports=ImportSpec(
        style="swift",
        statement_kinds=frozenset({"import_declaration"}),
    ),
    scopes=ScopeSpec(
        style="swift",
        scope_kinds={
            "source_file": "module",
            "function_declaration": "function",
            "init_declaration": "function",
            "lambda_literal": "lambda",
            "class_declaration": "class",
            "protocol_declaration": "class",
        },
        declaration_kinds=frozenset(
            {"function_declaration", "class_declaration", "protocol_declaration"}
        ),
        identifier_kind="simple_identifier",
        attribute_kind="navigation_expression",
    ),
)

SWIFT = LanguageProfile(
    name="swift",
    grammar="swift",
    extensions=frozenset({".swift"}),
    version=1,
    privacy="",
    privacy_rules=("modifier",),
    private_marker="modifiers",
    function_like=frozenset(
        {
            "function_declaration",
            "init_declaration",
            "deinit_declaration",
            "lambda_literal",
            "protocol_function_declaration",
        }
    ),
    class_like=frozenset({"class_declaration", "protocol_declaration"}),
    wrappers={},
    name_field="name",
    body_field="body",
    params_field="",
    parameter_kinds=frozenset({"parameter"}),
    receiver_kinds=frozenset(),
    comment_kinds=frozenset({"comment", "multiline_comment"}),
    string_kinds=frozenset(
        {"line_string_literal", "multi_line_string_literal", "raw_string_literal"}
    ),
    abstract_markers=frozenset(),
    abstract_kinds=frozenset({"protocol_declaration"}),
    metrics=SWIFT_METRICS,
)
