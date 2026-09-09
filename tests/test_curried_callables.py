"""A callable whose body *is* another callable, which used to be one entity instead of two.

`_descend` asked `_visit(body)`, and `_visit` iterates a node's *children*. So the body was
never itself tested for being a definition -- only what it contained was walked. For almost
every shape that is the same thing: an arrow whose body is a call expression, a Rust closure
inside a block, a Go func literal inside a function body. It differs for exactly one shape,
the curried one, where the body has no contents worth walking because the body *is* the next
callable::

    const add = (a) => (b) => a + b     // one entity, not two
    add = lambda a: lambda b: a + b     // one entity, not two

The inner callable was invisible to every metric and every ceiling: unmeasured, ungated, and
absent from the corpus counts. `graph.builder` is not part of the per-file cache key either,
so the fix ships with a `SCHEMA_VERSION` bump -- the same lesson `nom` and `wmc` taught.

The measured cost of having missed it was small and is worth recording rather than implying:
23 callables across nest's 15,020 and one in OXN's own source, none in the other four corpora.
Small, but it was the difference between "nest has 14,997 callables" being a measurement and
being an artefact of where the walk stopped.
"""

from __future__ import annotations

import pytest

CURRIED = {
    "typescript": ("m.ts", "export const add = (a: number) => (b: number) => a + b;\n"),
    "python": ("m.py", "add = lambda a: lambda b: a + b\n"),
}

#: Shapes that always worked, kept because the fix must not double-record them.
INTACT = {
    "typescript": ("n.ts", "const g = [1, 2].map((x) => [3].map((y) => x + y));\n"),
    "python": ("n.py", "def outer():\n    def inner():\n        return 1\n\n    return inner\n"),
    "rust": ("m.rs", "fn make() -> i32 {\n    let f = |a: i32| a + 1;\n    f(1)\n}\n"),
    "go": (
        "m.go",
        "package p\n\nfunc Make() int {\n\treturn func(a int) int { return a + 1 }(1)\n}\n",
    ),
}


def _callables(language: str, name: str, source: str) -> list[str]:
    from oxn.graph.builder import build_file
    from oxn.languages import get_parser
    from oxn.profiles import get_profile

    profile = get_profile(language)
    data = source.encode()
    tree = get_parser(language).parse(data)
    parsed = build_file(name, data, profile, tree.root_node)
    return [
        entity.qualified_name
        for entity in parsed.entities
        if entity.kind.value in {"function", "method", "lambda"}
    ]


@pytest.mark.parametrize("language", sorted(CURRIED))
def test_a_curried_callable_is_two_entities(language: str) -> None:
    """The regression. Both halves are real code and both can exceed a ceiling."""
    name, source = CURRIED[language]
    found = _callables(language, name, source)
    assert len(found) == 2, f"{language}: {found}"
    assert found[1].startswith(found[0] + "."), "the inner one belongs to the outer"


@pytest.mark.parametrize("language", sorted(INTACT))
def test_the_shapes_that_already_worked_are_not_double_recorded(language: str) -> None:
    """The other half of the fix: `_consider` must not record a node twice.

    An arrow whose body is a call, a nested `def`, a Rust closure and a Go func literal all
    produced two entities before the change and must still produce exactly two.
    """
    name, source = INTACT[language]
    found = _callables(language, name, source)
    assert len(found) == 2, f"{language}: {found}"
    assert len(set(found)) == 2, f"{language} recorded a duplicate: {found}"


def test_a_class_body_is_still_a_class_body() -> None:
    """`_descend`'s `inside_type` still reaches the body, so methods stay methods.

    The fix routes the body through `_consider` rather than `_visit`, and losing the flag
    there would have quietly reclassified every method in the project as a function.
    """
    from oxn.graph.builder import build_file
    from oxn.languages import get_parser
    from oxn.profiles import get_profile

    source = b"class A:\n    def f(self):\n        return 1\n"
    profile = get_profile("python")
    tree = get_parser("python").parse(source)
    kinds = {
        entity.qualified_name: entity.kind.value
        for entity in build_file("c.py", source, profile, tree.root_node).entities
    }
    assert kinds["c.A"] == "class"
    assert kinds["c.A.f"] == "method"
