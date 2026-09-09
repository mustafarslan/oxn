"""Six languages, one metric engine.

The acceptance gate for a language profile is not "it parses" but **it agrees**: identical
logic transliterated into every supported language must produce identical scores. That test
found two real bugs in TypeScript during P2 and three more here, because each grammar
spells the same construct differently and only comparison exposes the difference.
"""

from __future__ import annotations

import pytest

from oxn.languages import get_parser
from oxn.metrics import cognitive_complexity, cyclomatic_complexity
from oxn.profiles import PROFILES, get_profile

LANGUAGES = ("python", "typescript", "javascript", "go", "rust", "java")


def first_function(language: str, source: str):
    profile = get_profile(language)
    root = get_parser(language).parse(source.encode()).root_node
    assert not root.has_error, f"{language} fixture failed to parse"
    stack = [root]
    while stack:
        node = stack.pop(0)
        definition = profile.unwrap(node)
        if definition.type in profile.function_like:
            return profile, definition
        stack.extend(node.named_children)
    raise AssertionError(f"no function found in the {language} fixture")


def cognitive(language: str, source: str) -> int:
    profile, node = first_function(language, source)
    return cognitive_complexity(node, profile, function_name="f").score


def cyclomatic(language: str, source: str) -> int:
    profile, node = first_function(language, source)
    return cyclomatic_complexity(node, profile)


# ---- every language is fully configured -------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_profile_declares_the_tables_it_needs(language: str) -> None:
    metrics = get_profile(language).metrics
    assert metrics.cyclomatic.decision_points, "no decision points"
    assert metrics.cognitive.structural, "no structural increments"
    assert metrics.size.statement_kinds, "no statement kinds"
    assert metrics.halstead.operand_kinds, "no Halstead operands"
    assert metrics.imports.statement_kinds, "no import statements"
    assert metrics.scopes.scope_kinds, "no scopes"


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_profile_knows_its_functions_and_types(language: str) -> None:
    profile = get_profile(language)
    assert profile.function_like
    assert profile.class_like or language == "javascript"
    assert profile.comment_kinds


def test_the_registry_covers_every_launch_language() -> None:
    assert set(PROFILES) == set(LANGUAGES)


# ---- the transliteration matrix ------------------------------------------------------------

NEST3 = {
    "python": "def f(a, b):\n    if a > 0:\n        for i in range(a):\n            if b > i:\n                g()\n",
    "typescript": "function f(a: number, b: number) { if (a>0) { for (let i=0;i<a;i++) { if (b>i) { g(); } } } }",
    "javascript": "function f(a, b) { if (a>0) { for (let i=0;i<a;i++) { if (b>i) { g(); } } } }",
    "go": "package m\nfunc f(a int, b int) { if a>0 { for i := 0; i<a; i++ { if b>i { g() } } } }\n",
    "rust": "fn f(a: i32, b: i32) { if a>0 { for i in 0..a { if b>i { g(); } } } }",
    "java": "class C { void f(int a, int b) { if (a>0) { for (int i=0;i<a;i++) { if (b>i) { g(); } } } } }",
}

ELSE_IF_CHAIN = {
    "python": "def f(a):\n    if a: p()\n    elif a: q()\n    else: r()\n",
    "typescript": "function f(a){ if(a){p();} else if(a){q();} else {r();} }",
    "javascript": "function f(a){ if(a){p();} else if(a){q();} else {r();} }",
    "go": "package m\nfunc f(a int) { if a>0 { p() } else if a<0 { q() } else { r() } }\n",
    "rust": "fn f(a: i32) { if a>0 { p(); } else if a<0 { q(); } else { r(); } }",
    "java": "class C { void f(int a) { if (a>0) { p(); } else if (a<0) { q(); } else { r(); } } }",
}

ELSE_IF_NESTED = {
    "python": "def f(a):\n    if a: p()\n    elif a:\n        if a: q()\n",
    "typescript": "function f(a){ if(a){p();} else if(a){ if(a){q();} } }",
    "javascript": "function f(a){ if(a){p();} else if(a){ if(a){q();} } }",
    "go": "package m\nfunc f(a int) { if a>0 { p() } else if a<0 { if a>1 { q() } } }\n",
    "rust": "fn f(a: i32) { if a>0 { p(); } else if a<0 { if a>1 { q(); } } }",
    "java": "class C { void f(int a) { if (a>0) { p(); } else if (a<0) { if (a>1) { q(); } } } }",
}

BOOLEAN_RUNS = {
    "python": "def f(a,b,c,d,e,g):\n    if a and b and c or d or e and g: p()\n",
    "typescript": "function f(a,b,c,d,e,g){ if(a&&b&&c||d||e&&g){p();} }",
    "javascript": "function f(a,b,c,d,e,g){ if(a&&b&&c||d||e&&g){p();} }",
    "go": "package m\nfunc f(a,b,c,d,e,g bool) { if a&&b&&c||d||e&&g { p() } }\n",
    "rust": "fn f(a:bool,b:bool,c:bool,d:bool,e:bool,g:bool) { if a&&b&&c||d||e&&g { p(); } }",
    "java": "class C { void f(boolean a,boolean b,boolean c,boolean d,boolean e,boolean g){ if(a&&b&&c||d||e&&g){p();} } }",
}

LOOP_WITH_BRANCH = {
    "python": "def f(xs):\n    for x in xs:\n        if x: p()\n",
    "typescript": "function f(xs){ for (const x of xs) { if (x) { p(); } } }",
    "javascript": "function f(xs){ for (const x of xs) { if (x) { p(); } } }",
    "go": "package m\nfunc f(xs []int) { for _, x := range xs { if x>0 { p() } } }\n",
    "rust": "fn f(xs: Vec<i32>) { for x in xs { if x>0 { p(); } } }",
    "java": "class C { void f(int[] xs){ for (int x : xs) { if (x>0) { p(); } } } }",
}

MATRIX = [
    ("three levels of nesting", NEST3, 6),
    ("if / else-if / else", ELSE_IF_CHAIN, 3),
    ("else-if containing an if", ELSE_IF_NESTED, 4),
    ("a && b && c || d || e && g", BOOLEAN_RUNS, 4),
    ("loop containing a branch", LOOP_WITH_BRANCH, 3),
]


@pytest.mark.parametrize(("label", "sources", "expected"), MATRIX)
@pytest.mark.parametrize("language", LANGUAGES)
def test_cognitive_complexity_agrees_across_languages(
    label: str, sources: dict[str, str], expected: int, language: str
) -> None:
    """Three grammars spell `else if` three different ways; the score must not notice.

    Python has a dedicated `elif_clause`; TypeScript, JavaScript and Rust wrap the next
    `if` in an `else_clause`; Go and Java put it directly in the `alternative` field with no
    `else` node at all.
    """
    assert cognitive(language, sources[language]) == expected, label


# ---- per-language rules that differ, deliberately -------------------------------------------


def test_go_has_no_while_and_no_catch() -> None:
    """Go's `for` covers every loop and it has no exceptions -- the profile must not invent them."""
    metrics = get_profile("go").metrics
    assert "while_statement" not in metrics.cyclomatic.decision_points
    assert "catch_clause" not in metrics.cognitive.structural
    assert cognitive("go", "package m\nfunc f(a int) { for a>0 { p() } }\n") == 1


def test_go_select_is_a_multiway_branch() -> None:
    source = "package m\nfunc f(ch chan int) { select { case <-ch: p() } }\n"
    assert cognitive("go", source) == 1


def test_rust_question_mark_is_a_branch() -> None:
    """`?` returns early on an error, so it branches. Lizard disagrees; see divergences.md."""
    assert cyclomatic("rust", "fn f() -> R { let x = g()?; Ok(x) }") == 2
    assert cyclomatic("rust", "fn f() -> R { Ok(1) }") == 1


def test_rust_match_is_exhaustive_so_n_arms_are_n_minus_one_decisions() -> None:
    """A two-arm match is exactly an if/else and must score exactly the same.

    Rust's compiler enforces exhaustiveness, so there is no implicit "nothing matched" path.
    A C-style `switch` is different: its cases plus the fall-through give n paths for n
    cases, which is why `default` is excluded there instead.
    """
    two_arms = "fn f(a: R) -> i32 { match a { Ok(x) => 1, Err(e) => 2 } }"
    if_else = "fn f(a: i32) -> i32 { if a>0 { 1 } else { 2 } }"
    assert cyclomatic("rust", two_arms) == cyclomatic("rust", if_else) == 2
    assert cyclomatic("rust", "fn f(a: i32) -> i32 { match a { 1=>1, 2=>2, _=>3 } }") == 3


def test_nested_matches_do_not_double_count() -> None:
    source = "fn f(a: R) -> i32 { match a { Ok(x) => match x { 1 => 1, _ => 2 }, Err(_) => 0 } }"
    assert cyclomatic("rust", source) == 3


def test_rust_if_let_is_one_branch_not_two() -> None:
    """Counting the `let_condition` as well as the `if` doubles every `if let`."""
    assert cyclomatic("rust", "fn f(a: O) -> i32 { if let Some(x) = a { x } else { 0 } }") == 2


def test_a_catch_all_case_is_not_a_decision() -> None:
    """`case _` is the fall-through, like `default` in a switch -- not a branch of its own."""
    with_wildcard = "def f(a):\n    match a:\n        case 1: pass\n        case _: pass\n"
    if_else = "def f(a):\n    if a: pass\n    else: pass\n"
    assert cyclomatic("python", with_wildcard) == cyclomatic("python", if_else) == 2


def test_java_switch_counts_cases_but_not_default() -> None:
    source = (
        "class C { int f(int a){ switch(a){ case 1: return 1; case 2: return 2;"
        " default: return 3; } } }"
    )
    assert cyclomatic("java", source) == 3  # base + two cases
    assert cognitive("java", source) == 1  # a switch and all its cases are one increment


@pytest.mark.parametrize(
    ("language", "source"),
    [
        ("go", 'package m\n\nimport (\n\t"fmt"\n\talias "os"\n)\n'),
        ("rust", "use std::collections::HashMap;\nuse std::fmt::{self, Display};\n"),
        ("java", "import java.util.List;\nimport static java.lang.Math.max;\n"),
    ],
)
def test_imports_are_extracted_for_every_language(language: str, source: str) -> None:
    from oxn.graph.imports import extract_imports

    profile = get_profile(language)
    root = get_parser(language).parse(source.encode()).root_node
    assert extract_imports(root, profile), f"{language} imports were not extracted"


@pytest.mark.parametrize("language", LANGUAGES)
def test_scopes_build_without_error_for_every_language(language: str) -> None:
    from oxn.resolve.scopes import build_scopes

    sources = {
        "python": "def f(a):\n    b = a\n    return b\n",
        "typescript": "function f(a: number) { const b = a; return b; }",
        "javascript": "function f(a) { const b = a; return b; }",
        "go": "package m\nfunc f(a int) int { b := a; return b }\n",
        "rust": "fn f(a: i32) -> i32 { let b = a; b }",
        "java": "class C { int f(int a) { int b = a; return b; } }",
    }
    profile = get_profile(language)
    root = get_parser(language).parse(sources[language].encode()).root_node
    tree = build_scopes(root, profile)
    assert tree.all_scopes(), f"{language} produced no scopes"


# ---- the same agreement, for every metric that is a property of the logic -------------------
#
# The matrix above proved cognitive complexity agrees, and nothing proved anything else did.
# Two of them did not, and both were found by transliterating one function into six languages
# rather than by reading any of them:
#
#   max_nesting_depth   an `else if` read as two levels wherever the grammar wraps it in an
#                       `else_clause`, so TypeScript and Rust scored 4 where Python, Java and
#                       Go scored 3 -- on a *gated* ceiling, which means the same code was
#                       accepted or rejected according to what it was written in.
#   exit_points         Rust ends a function on a bare expression and leaves through `?`, and
#                       neither counted, so it scored 4 against everyone else's 5.
#
# `sloc`, `lloc` and the Halstead family are deliberately absent: braces are real lines and
# real tokens, so Python differing there is the measurement working.

STRUCTURAL = ("cyclomatic_complexity", "cognitive_complexity", "max_nesting_depth", "exit_points")

EARLY_RETURNS = {
    "python": "def f(a, b):\n    if a:\n        return 1\n    if b:\n        return 2\n    return 3\n",
    "typescript": "function f(a,b){ if(a){ return 1; } if(b){ return 2; } return 3; }",
    "javascript": "function f(a,b){ if(a){ return 1; } if(b){ return 2; } return 3; }",
    "go": "package m\nfunc f(a, b bool) int { if a { return 1 }\n if b { return 2 }\n return 3 }\n",
    "rust": "fn f(a: bool, b: bool) -> i32 { if a { return 1; } if b { return 2; } 3 }",
    "java": "class C { int f(boolean a, boolean b){ if(a){ return 1; } if(b){ return 2; } return 3; } }",
}

STRUCTURAL_MATRIX = [
    ("three levels of nesting", NEST3),
    ("if / else-if / else", ELSE_IF_CHAIN),
    ("else-if containing an if", ELSE_IF_NESTED),
    ("loop containing a branch", LOOP_WITH_BRANCH),
    ("three exits, one implicit in Rust", EARLY_RETURNS),
]


def _structural(language: str, source: str) -> dict[str, int]:
    from oxn.metrics import exit_points, max_nesting_depth

    profile, node = first_function(language, source)
    return {
        "cyclomatic_complexity": cyclomatic_complexity(node, profile),
        "cognitive_complexity": cognitive_complexity(node, profile, function_name="f").score,
        "max_nesting_depth": max_nesting_depth(node, profile),
        "exit_points": exit_points(node, profile),
    }


@pytest.mark.parametrize(("label", "sources"), STRUCTURAL_MATRIX)
@pytest.mark.parametrize("metric", STRUCTURAL)
def test_every_structural_metric_agrees_across_languages(
    label: str, sources: dict[str, str], metric: str
) -> None:
    """One shape, six grammars, one number. Disagreement is the bug, whatever the number is.

    Asserting agreement rather than a value is deliberate: the value is a judgement that the
    per-metric tests already pin, and *which* number is right does not change the fact that
    six answers to one question means at least five are wrong.
    """
    scored = {language: _structural(language, sources[language])[metric] for language in LANGUAGES}
    assert len(set(scored.values())) == 1, f"{label}: {metric} disagrees -- {scored}"


def test_rust_leaves_through_its_tail_expression_and_through_a_question_mark() -> None:
    """The two Rust exits written with no `return`, which is why it read one short.

    `?` is not a stylistic detail: Go spells the same control flow `if err != nil { return }`
    and it has always been counted, so leaving `?` out made the two languages incomparable on
    the most common error-handling shape either of them has.
    """
    from oxn.metrics import exit_points

    profile, node = first_function("rust", "fn f(a: i32) -> i32 { let x = g(a)?; x + 1 }")
    assert exit_points(node, profile) == 2

    profile, node = first_function("rust", "fn f(a: i32) -> i32 { return a; }")
    assert exit_points(node, profile) == 1, "an explicit return must not also count a tail"
