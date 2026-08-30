"""Cognitive Complexity against the specification.

Fixtures marked WHITE PAPER are transliterated from the worked examples in
*Cognitive Complexity: a new way of measuring understandability*, G. Ann Campbell,
SonarSource, v1.7 (29 August 2023), used here to verify an independent implementation.
Their stated scores are the assertions.
"""

from __future__ import annotations

import pytest

from oxn.languages import get_parser
from oxn.metrics import cognitive_complexity
from oxn.profiles import get_profile


def score(language: str, source: str, name: str = "f") -> int:
    profile = get_profile(language)
    root = get_parser(language).parse(source.encode()).root_node
    fn = profile.unwrap(root.named_children[0])
    return cognitive_complexity(fn, profile, function_name=name).score


# ---- the specification's own examples --------------------------------------------------


def test_white_paper_my_method_scores_9() -> None:
    """WHITE PAPER: try is ignored, catch is structural, nesting accumulates."""
    source = """
function myMethod(condition1, condition2) {
  try {
    if (condition1) {
      for (let i = 0; i < 10; i++) {
        while (condition2) { }
      }
    }
  } catch (e) {
    if (condition2) { }
  }
}
"""
    assert score("javascript", source.strip(), "myMethod") == 9


def test_white_paper_my_method2_scores_2() -> None:
    """WHITE PAPER: a lambda scores nothing itself but nests what is inside it."""
    source = """
function myMethod2(condition1) {
  const r = () => {
    if (condition1) { }
  };
  if (condition1) { }
}
"""
    # The declarative-function exception does not apply: a structural statement sits at the
    # function's top level, so the lambda nests normally -- +1 for the outer `if`, +2 inside.
    assert score("javascript", source.strip(), "myMethod2") == 3


def test_white_paper_boolean_sequences() -> None:
    """WHITE PAPER: one increment per *run* of like operators, not per operator."""
    assert score("javascript", "function f(a,b,c,d,e,g){ if (a && b && c || d || e && g) {} }") == 4
    assert score("javascript", "function f(a,b,c){ if (a && !(b && c)) {} }") == 3
    assert score("javascript", "function f(a,b){ if (a && b) {} }") == 2
    assert score("javascript", "function f(a,b,c,d){ if (a || b || c || d) {} }") == 2


def test_white_paper_switch_and_all_cases_score_one() -> None:
    """WHITE PAPER: getWords -- a switch is taken in at a glance, unlike an if/else chain."""
    source = """
function getWords(number) {
  switch (number) {
    case 1: return "one";
    case 2: return "a couple";
    case 3: return "a few";
    default: return "lots";
  }
}
"""
    assert score("javascript", source.strip(), "getWords") == 1


def test_white_paper_python_decorator_exception() -> None:
    """WHITE PAPER Appendix A: a function holding only a nested function and a return.

    Such a function does not increment the nesting level, so Python's decorator idiom is
    not penalised. The three cases and their stated totals are 1, 2 and 1.
    """
    a_decorator = """
def a_decorator(a, b):
    def inner(func):
        if condition:
            print(b)
        func()
    return inner
"""
    not_a_decorator = """
def not_a_decorator(a, b):
    my_var = a * b
    def inner(func):
        if condition:
            print(b)
        func()
    return inner
"""
    decorator_generator = """
def decorator_generator(a):
    def generator(func):
        def decorator(func):
            if condition:
                print(b)
            return func()
        return decorator
    return generator
"""
    assert score("python", a_decorator.strip(), "a_decorator") == 1
    assert score("python", not_a_decorator.strip(), "not_a_decorator") == 2
    assert score("python", decorator_generator.strip(), "decorator_generator") == 1


def test_white_paper_javascript_declarative_function_exception() -> None:
    """WHITE PAPER Appendix A: an outer function used purely as a namespace is ignored."""
    declarative = """
function outer() {
  var foo;
  bar.myFun = function () {
    if (condition) { }
  };
}
"""
    non_declarative = """
function outer() {
  var foo;
  if (condition) { }
  bar.myFun = function () {
    if (condition) { }
  };
}
"""
    assert score("javascript", declarative.strip(), "outer") == 1
    assert score("javascript", non_declarative.strip(), "outer") == 3


def test_idea_md_example_scores_6() -> None:
    """The worked example in this project's own idea.md, in both launch languages."""
    python = (
        "def example(a, b):\n"
        "    if a > 0:\n"
        "        for i in range(a):\n"
        "            if b > i:\n"
        "                do_something()\n"
    )
    typescript = (
        "function example(a,b){ if(a>0){ for(let i=0;i<a;i++){ if(b>i){ doSomething(); } } } }"
    )
    assert score("python", python, "example") == 6
    assert score("typescript", typescript, "example") == 6


# ---- rules that reimplementations get wrong --------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("def f(a):\n    if a: pass\n", 1),
        ("def f(a):\n    if a: pass\n    else: pass\n", 2),
        ("def f(a):\n    if a: pass\n    elif a: pass\n    else: pass\n", 3),
        # A loop's `else` is not an if/else and must not count.
        ("def f(a):\n    for i in a: pass\n    else: pass\n", 1),
        ("def f(a):\n    while a: pass\n    else: pass\n", 1),
        # try and finally are ignored entirely; catch is structural.
        ("def f():\n    try: pass\n    finally: pass\n", 0),
        ("def f():\n    try: pass\n    except E: pass\n", 1),
        ("def f():\n    try: pass\n    except E: pass\n    except F: pass\n", 2),
        # A match and all its cases together incur one increment.
        ("def f(a):\n    match a:\n        case 1: pass\n        case _: pass\n", 1),
        # Direct recursion.
        ("def f(a):\n    return f(a - 1)\n", 1),
    ],
)
def test_python_increment_rules(source: str, expected: int) -> None:
    assert score("python", source, "f") == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # Only labelled and multi-level jumps increment.
        ("function f(a){ for(const x of a){ break; } }", 1),
        ("function f(a){ for(const x of a){ continue; } }", 1),
        ("function f(a){ for(const x of a){ return x; } }", 1),
        ("function f(a){ outer: for(const x of a){ for(const y of a){ break outer; } } }", 4),
        ("function f(a){ outer: for(const x of a){ for(const y of a){ continue outer; } } }", 4),
        # A comparison is a binary_expression too, and must not score as a boolean sequence.
        ("function f(a){ if (a > 0) {} }", 1),
        ("function f(a){ if (a === 0 && a !== 1) {} }", 2),
    ],
)
def test_javascript_increment_rules(source: str, expected: int) -> None:
    assert score("javascript", source, "f") == expected


# ---- cross-language equivalence --------------------------------------------------------


@pytest.mark.parametrize(
    ("python_source", "ts_source", "expected"),
    [
        (
            "def f(a):\n    if a:\n        pass\n    elif a:\n        if a:\n            pass\n",
            "function f(a){ if(a){} else if(a){ if(a){} } }",
            4,
        ),
        (
            "def f(a):\n    if a:\n        pass\n    else:\n        if a:\n            pass\n",
            "function f(a){ if(a){} else { if(a){} } }",
            4,
        ),
        (
            "def f(a):\n    if a: pass\n    elif a: pass\n    else: pass\n",
            "function f(a){ if(a){} else if(a){} else {} }",
            3,
        ),
        (
            "def f(a,b):\n    if a:\n        for i in a:\n            if b: pass\n",
            "function f(a,b){ if(a){ for(const i of a){ if(b){} } } }",
            6,
        ),
        (
            "def f(a):\n    try:\n        if a: pass\n    except E:\n        if a: pass\n",
            "function f(a){ try { if(a){} } catch(e) { if(a){} } }",
            4,
        ),
    ],
)
def test_languages_agree_on_transliterated_code(
    python_source: str, ts_source: str, expected: int
) -> None:
    """The strongest test of language-agnosticism, and the one that found two real bugs.

    Python and TypeScript spell `else if` completely differently -- a dedicated
    `elif_clause` versus an `else_clause` wrapping an `if_statement` -- so identical logic
    must still score identically.
    """
    assert score("python", python_source, "f") == expected
    assert score("typescript", ts_source, "f") == expected


# ---- the explanation trail -------------------------------------------------------------


def test_every_point_of_the_score_is_explained() -> None:
    """The trail is the product: an agent cannot act on a bare number."""
    profile = get_profile("python")
    source = "def f(a, b):\n    if a:\n        for i in a:\n            if b: pass\n"
    root = get_parser("python").parse(source.encode()).root_node
    result = cognitive_complexity(
        profile.unwrap(root.named_children[0]), profile, function_name="f"
    )

    assert result.score == sum(inc.amount for inc in result.increments)
    assert [inc.amount for inc in result.increments] == [1, 2, 3]
    assert [inc.line for inc in result.increments] == [2, 3, 4]
    assert "nested 2 deep" in result.explain()[-1]
