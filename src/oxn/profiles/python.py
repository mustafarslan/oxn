"""Python language profile.

Node kinds verified against the ``tree-sitter-python`` grammar shipped in
``tree-sitter-language-pack`` 1.15.8 on 2026-08-29, not taken from documentation.
"""

from __future__ import annotations

from oxn.profiles.base import LanguageProfile, Wrapper
from oxn.profiles.spec import (
    CognitiveSpec,
    CyclomaticSpec,
    HalsteadSpec,
    ImportSpec,
    MetricSpec,
    SizeSpec,
)

_BOOLEAN_OPS = frozenset({"and", "or"})

PYTHON_METRICS = MetricSpec(
    cyclomatic=CyclomaticSpec(
        # OXN's rule: count what branches the control-flow graph.
        #   assert     -> counted (radon agrees, lizard does not): it raises or continues.
        #   finally    -> not counted (radon agrees, lizard does not): unconditional.
        #   loop else  -> not counted (lizard agrees, radon does not): OXN counts no `else`.
        #   case       -> counted per case (lizard agrees, radon does not).
        # Every divergence is recorded in docs/divergences.md.
        decision_points=frozenset(
            {
                "if_statement",
                "elif_clause",
                "conditional_expression",
                "for_statement",
                "while_statement",
                "except_clause",
                "assert_statement",
                "case_clause",
                # Comprehension clauses are loops and filters like any other.
                "for_in_clause",
                "if_clause",
            }
        ),
        boolean_node="boolean_operator",
        boolean_operators=_BOOLEAN_OPS,
    ),
    cognitive=CognitiveSpec(
        structural=frozenset(
            {
                "if_statement",
                "conditional_expression",
                "for_statement",
                "while_statement",
                "except_clause",
                "match_statement",
            }
        ),
        # `elif`/`else`: +1, no nesting increment, but they raise the nesting level.
        hybrid=frozenset({"elif_clause", "else_clause"}),
        # Lambdas and nested functions score nothing themselves but nest what is inside.
        nesting_only=frozenset({"lambda", "function_definition"}),
        fundamental=frozenset(),
        # The specification ignores `try` and `finally` altogether. Python has no labelled
        # break, so it never receives a jump increment.
        ignored=frozenset({"try_statement", "finally_clause"}),
        boolean_node="boolean_operator",
        boolean_operators=_BOOLEAN_OPS,
        comprehension_kinds=frozenset(
            {
                "list_comprehension",
                "set_comprehension",
                "dictionary_comprehension",
                "generator_expression",
            }
        ),
        comprehension_loop_kinds=frozenset({"for_in_clause"}),
        comprehension_filter_kinds=frozenset({"if_clause"}),
        hybrid_parents=frozenset({"if_statement"}),
        python_decorator_exception=True,
        call_kinds=frozenset({"call"}),
        callee_field="function",
    ),
    size=SizeSpec(
        statement_kinds=frozenset(
            {
                "expression_statement",
                "return_statement",
                "pass_statement",
                "raise_statement",
                "assert_statement",
                "delete_statement",
                "import_statement",
                "global_statement",
                "import_from_statement",
                "print_statement",
                "break_statement",
                "exec_statement",
                "continue_statement",
                "if_statement",
                "for_statement",
                "while_statement",
                "try_statement",
                "with_statement",
                "match_statement",
                "function_definition",
                "class_definition",
                "nonlocal_statement",
                "future_import_statement",
            }
        ),
        return_kinds=frozenset({"return_statement", "raise_statement"}),
        docstrings_are_comments=True,
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
        style="python",
        statement_kinds=frozenset({"import_statement", "import_from_statement"}),
        module_field="module_name",
        name_field="name",
        relative_kinds=frozenset({"relative_import"}),
        wildcard_kinds=frozenset({"wildcard_import"}),
        alias_kinds=frozenset({"aliased_import"}),
        string_kinds=frozenset({"string"}),
        type_only_token=None,
        dynamic_callees=frozenset({"importlib.import_module", "__import__"}),
        call_kinds=frozenset({"call"}),
    ),
)


PYTHON = LanguageProfile(
    name="python",
    grammar="python",
    extensions=frozenset({".py", ".pyi"}),
    version=1,
    function_like=frozenset({"function_definition", "lambda"}),
    class_like=frozenset({"class_definition"}),
    # A decorated function is `decorated_definition > [definition] function_definition`,
    # so the decorators belong to the wrapper's span and the entity is the inner node.
    wrappers={
        "decorated_definition": Wrapper("decorated_definition", "definition"),
    },
    name_field="name",
    body_field="body",
    params_field="parameters",
    parameter_kinds=frozenset(
        {
            "identifier",
            "typed_parameter",
            "default_parameter",
            "typed_default_parameter",
            "list_splat_pattern",
            "dictionary_splat_pattern",
            "keyword_separator",
            "positional_separator",
        }
    ),
    # Python has no syntactic receiver: `self` is an ordinary first parameter. Receiver
    # identification is therefore semantic (is this a method? is this its first param?)
    # and lives in the LCOM work of P6, not here.
    receiver_kinds=frozenset(),
    comment_kinds=frozenset({"comment"}),
    string_kinds=frozenset({"string", "concatenated_string"}),
    abstract_markers=frozenset(
        {"abstractmethod", "abstractproperty", "ABC", "ABCMeta", "Protocol"}
    ),
    abstract_kinds=frozenset(),
    metrics=PYTHON_METRICS,
)
