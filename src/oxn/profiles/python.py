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
    ScopeSpec,
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
        catch_all_kinds=frozenset({"case_clause"}),
        catch_all_pattern_kinds=frozenset({"case_pattern"}),
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
        statement_containers=frozenset({"block", "module"}),
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
    scopes=ScopeSpec(
        static_markers=frozenset({"staticmethod"}),
        style="python",
        scope_kinds={
            "module": "module",
            "class_definition": "class",
            "function_definition": "function",
            "lambda": "lambda",
            "list_comprehension": "comprehension",
            "set_comprehension": "comprehension",
            "dictionary_comprehension": "comprehension",
            "generator_expression": "comprehension",
        },
        declaration_kinds=frozenset({"function_definition", "class_definition"}),
        parameter_containers=frozenset({"parameters", "lambda_parameters"}),
        assignment_kinds=frozenset(
            {
                "assignment",
                "augmented_assignment",
                "for_statement",
                "for_in_clause",
                "named_expression",
            }
        ),
        alias_kinds=frozenset({"as_pattern", "aliased_import", "except_clause"}),
        rebinding_kinds=frozenset({"global_statement", "nonlocal_statement"}),
        attribute_kind="attribute",
        attribute_object_field="object",
        attribute_name_field="attribute",
        identifier_kind="identifier",
    ),
)


PYTHON = LanguageProfile(
    name="python",
    grammar="python",
    extensions=frozenset({".py", ".pyi"}),
    version=6,
    privacy="underscore",
    dispatch="dunder",
    privacy_rules=("name",),
    function_like=frozenset({"function_definition", "lambda"}),
    supertype_fields=frozenset({"superclasses"}),
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
        }
    ),
    # `*` and `/` are *markers*, not parameters -- nobody passes them. Listing them here
    # charged every keyword-only signature one extra parameter, so `f(a, b, *, c, d, e)`
    # measured six against a ceiling of five. That is a bias against the more explicit
    # style, which is the opposite of what the ceiling is for.
    # Python has no syntactic receiver: `self` is an ordinary first parameter, so there is
    # no node kind to match. `receiver_names` below carries it instead.
    receiver_kinds=frozenset(),
    receiver_names=frozenset({"self", "cls"}),
    comment_kinds=frozenset({"comment"}),
    string_kinds=frozenset({"string", "concatenated_string"}),
    abstract_markers=frozenset(
        {"abstractmethod", "abstractproperty", "ABC", "ABCMeta", "Protocol"}
    ),
    abstract_kinds=frozenset(),
    metrics=PYTHON_METRICS,
)
