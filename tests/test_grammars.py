"""Every launch grammar must load and parse clean source without error nodes.

This is the operational verification of the ``tree-sitter-language-pack`` dependency
(ADR-0001): the package advertises 371 grammars fetched lazily, and this test proves the
six OXN actually launches with work on the running interpreter. It also de-risks P1 --
the ``LanguageProfile`` work assumes these parsers exist and behave.
"""

from __future__ import annotations

import pytest

from oxn.languages import LAUNCH_LANGUAGES, get_parser, language_for_path

#: Minimal, unambiguous source per language, exercising a function and a branch -- the two
#: constructs every Tier-1 metric is built on.
SNIPPETS: dict[str, str] = {
    "python": "def f(a):\n    if a > 0:\n        return a\n    return -a\n",
    "typescript": "function f(a: number): number {\n  if (a > 0) { return a; }\n  return -a;\n}\n",
    "javascript": "function f(a) {\n  if (a > 0) { return a; }\n  return -a;\n}\n",
    "go": "package main\n\nfunc f(a int) int {\n\tif a > 0 {\n\t\treturn a\n\t}\n\treturn -a\n}\n",
    "rust": "fn f(a: i32) -> i32 {\n    if a > 0 { return a; }\n    -a\n}\n",
    "java": "class C {\n  int f(int a) {\n    if (a > 0) { return a; }\n    return -a;\n  }\n}\n",
}


def _has_error(node) -> bool:
    """True if the subtree contains an ERROR or MISSING node.

    docs/metrics.md section 9.4 makes this a hard rule: a tree with error nodes must
    suppress the affected metrics rather than emit a silently wrong number.
    """
    if node.type == "ERROR" or node.is_missing:
        return True
    return any(_has_error(child) for child in node.children)


@pytest.mark.parametrize("language", sorted(LAUNCH_LANGUAGES))
def test_grammar_parses_clean_source(language: str) -> None:
    parser = get_parser(language)
    tree = parser.parse(SNIPPETS[language].encode())

    assert tree.root_node.type not in {"ERROR", ""}
    assert not _has_error(tree.root_node), f"{language} grammar produced error nodes"
    assert tree.root_node.end_byte == len(SNIPPETS[language].encode())


@pytest.mark.parametrize("language", sorted(LAUNCH_LANGUAGES))
def test_grammar_finds_a_function(language: str) -> None:
    """Sanity check that the tree has real structure, not one flat node."""
    parser = get_parser(language)
    tree = parser.parse(SNIPPETS[language].encode())

    kinds = set()
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        kinds.add(node.type)
        stack.extend(node.children)

    assert any("function" in k or "method" in k or "declaration" in k for k in kinds), (
        f"{language}: no function-like node found among {sorted(kinds)}"
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("a/b/c.py", "python"),
        ("src/index.ts", "typescript"),
        ("src/index.tsx", "typescript"),
        ("main.go", "go"),
        ("lib.rs", "rust"),
        ("Main.java", "java"),
        ("README.md", None),
        ("noextension", None),
    ],
)
def test_language_detection(path: str, expected: str | None) -> None:
    assert language_for_path(path) == expected
