"""Python language profile.

Node kinds verified against the ``tree-sitter-python`` grammar shipped in
``tree-sitter-language-pack`` 1.15.8 on 2026-08-29, not taken from documentation.
"""

from __future__ import annotations

from oxn.profiles.base import LanguageProfile, Wrapper

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
)
