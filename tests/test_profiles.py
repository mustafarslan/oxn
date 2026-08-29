"""The LanguageProfile contract, and the grammar facts it encodes.

Several assertions here restate findings verified directly against the grammars. They are
tests rather than comments because the whole metric engine is built on them, and a grammar
upgrade that changes a node kind must fail loudly rather than silently skew a metric.
"""

from __future__ import annotations

import pytest

from oxn.languages import get_parser
from oxn.profiles import PROFILES, UnsupportedLanguageError, get_profile, profile_for_path


def test_every_profile_has_a_grammar() -> None:
    from oxn.languages import LAUNCH_LANGUAGES

    for name, profile in PROFILES.items():
        assert profile.grammar in LAUNCH_LANGUAGES.values(), name


def test_extensions_do_not_collide() -> None:
    seen: dict[str, str] = {}
    for profile in PROFILES.values():
        for ext in profile.extensions:
            assert ext not in seen, f"{ext} claimed by {seen.get(ext)} and {profile.name}"
            seen[ext] = profile.name


def test_unknown_language_is_explicit() -> None:
    with pytest.raises(UnsupportedLanguageError):
        get_profile("cobol")


def test_profile_for_path() -> None:
    assert profile_for_path("a/b.py").name == "python"
    assert profile_for_path("a/b.tsx").name == "typescript"
    assert profile_for_path("a/b.mjs").name == "javascript"
    assert profile_for_path("a/b.md") is None


# ---- grammar facts the metric engine depends on ------------------------------------


def test_python_if_statement_has_several_alternative_fields() -> None:
    """``elif`` and ``else`` are siblings under one field name.

    Reading them with ``child_by_field_name`` returns only the first, which would drop every
    ``else`` in the codebase. docs/metrics.md section 3.2 documents this.
    """
    src = b"def f(a):\n    if a:\n        x()\n    elif a:\n        y()\n    else:\n        z()\n"
    root = get_parser("python").parse(src).root_node
    if_stmt = _find(root, "if_statement")
    alternatives = if_stmt.children_by_field_name("alternative")
    assert [n.type for n in alternatives] == ["elif_clause", "else_clause"]


def test_typescript_else_if_lives_inside_an_else_clause() -> None:
    """The else-branch is always an ``else_clause``; ``else if`` is what it contains.

    The cognitive-complexity rule is therefore "look inside the else_clause", not "test the
    alternative node's type".
    """
    src = b"function f(a){ if(a){x()} else if(a){y()} else {z()} }"
    root = get_parser("typescript").parse(src).root_node
    if_stmt = _find(root, "if_statement")
    alternative = if_stmt.child_by_field_name("alternative")
    assert alternative is not None
    assert alternative.type == "else_clause"
    assert [c.type for c in alternative.named_children] == ["if_statement"]


def test_typescript_abstract_class_is_its_own_node_kind() -> None:
    """Not ``class_declaration`` with a modifier -- abstractness (P4) reads the kind."""
    src = b"abstract class A {}\nclass B {}\n"
    root = get_parser("typescript").parse(src).root_node
    kinds = [c.type for c in root.named_children]
    assert kinds == ["abstract_class_declaration", "class_declaration"]
    assert "abstract_class_declaration" in get_profile("typescript").abstract_kinds


def test_python_decorated_definition_wraps_the_function() -> None:
    src = b"@d\ndef f():\n    pass\n"
    root = get_parser("python").parse(src).root_node
    wrapper = _find(root, "decorated_definition")
    profile = get_profile("python")
    assert profile.unwrap(wrapper).type == "function_definition"
    assert profile.entity_name(profile.unwrap(wrapper)) == "f"


def test_parameter_names_across_python_forms() -> None:
    src = b"def f(a, b: int, c=1, d: int = 2, *args, **kw):\n    pass\n"
    root = get_parser("python").parse(src).root_node
    fn = _find(root, "function_definition")
    assert get_profile("python").parameter_names(fn) == ["a", "b", "c", "d", "args", "kw"]


def test_parameter_names_across_typescript_forms() -> None:
    src = b"function f(a: number, b?: string, ...rest: number[]) {}"
    root = get_parser("typescript").parse(src).root_node
    fn = _find(root, "function_declaration")
    assert get_profile("typescript").parameter_names(fn) == ["a", "b", "rest"]


def _find(node, kind: str):
    stack = [node]
    while stack:
        current = stack.pop(0)
        if current.type == kind:
            return current
        stack.extend(current.named_children)
    raise AssertionError(f"no {kind} in tree")
