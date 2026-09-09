"""A callable bound to a name is not anonymous, and both readers must agree which name.

`const handler = () => {}` produced an entity with `name=None`. `_declared_in` skips those,
so nothing could resolve a call to `handler()` -- and `scip/join.py` matched entities to SCIP
symbols through the definition's *name field*, which an arrow function does not have, so the
entity was never graded either.

**Those two halves have to move together**, and doing only the first is measurable proof of
it: naming the entities without teaching the join produced entities that could be looked up
by name and never scored, and the corpora came back very slightly *worse* -- new candidates
diluting existing lookups, with no new graded site anywhere. `LanguageProfile.name_node` is
the single answer both readers now ask for. On `typescript-nest` that took graded call sites
from 2,172 to 2,362, precision from 66.9% to 69.2%, and confident recall from 53.2% to 56.7%,
with precision-when-certain still 100%.

What stays anonymous stays anonymous on purpose: an inline callback (`items.map(x => x.id)`)
has no name to be called by, and 1,551 of nest's remaining 1,785 unentitied call sites are
exactly that.

**A name is not a kind.** These callables are anonymous *definitions* that a binding gives a
name to, and they are labelled `lambda` in every language that has them -- which Go and Rust
were not, reading as ordinary functions until the two node kinds were added to the builder's
anonymous set. The row for Go's `:=` is here because the `var` form was named and the short
form was not, so one language disagreed with itself.
"""

from __future__ import annotations

import pytest

from oxn.graph.builder import build_file
from oxn.languages import get_parser
from oxn.profiles import get_profile


def named(language: str, source: str) -> list[tuple[str, str | None]]:
    profile = get_profile(language)
    data = source.encode()
    root = get_parser(profile.grammar).parse(data).root_node
    return [
        (entity.kind.value, entity.name)
        for entity in build_file("m", data, profile, root).entities
        if entity.kind.value != "module"
    ]


@pytest.mark.parametrize(
    ("language", "source", "expected"),
    [
        ("typescript", "const arrow = () => {};\n", [("lambda", "arrow")]),
        ("typescript", "const fn = function () {};\n", [("lambda", "fn")]),
        ("python", "outer = lambda: 1\n", [("lambda", "outer")]),
        # Go puts the value inside an `expression_list`, so the declarator is a grandparent.
        ("go", "package m\nvar Fn = func() {}\n", [("lambda", "Fn")]),
        (
            "go",
            "package m\nfunc h() { g := func() {} ; _ = g }\n",
            [("function", "h"), ("lambda", "g")],
        ),
        ("rust", "fn f() { let g = |x| x; }\n", [("function", "f"), ("lambda", "g")]),
    ],
)
def test_a_callable_bound_to_a_name_takes_it(
    language: str, source: str, expected: list[tuple[str, str | None]]
) -> None:
    assert named(language, source) == expected


@pytest.mark.parametrize(
    ("language", "source"),
    [
        # An inline callback has no name to be called by, and must not borrow one.
        ("typescript", "items.map(x => x.id);\n"),
        # An attribute is not a bare name: nothing can call `m()` and mean this.
        ("typescript", "obj.m = () => {};\n"),
        # Names bind positionally here, so `a` is not this function's name.
        ("go", "package m\nvar a, b = 1, func() {}\n"),
    ],
)
def test_what_stays_anonymous(language: str, source: str) -> None:
    """**Inverted on purpose.** Each of these is a callable with no name a caller could
    write, and inventing one would put a wrong answer in the bare-name table rather than
    leaving a gap that is visible."""
    assert [name for _kind, name in named(language, source) if name is not None] == []


def test_the_join_and_the_entity_agree_on_which_node_names_it() -> None:
    """The half that was missing, asserted as the property rather than the fix.

    `scip/join.py` looks up the SCIP occurrence *at this node's position*. If it asks a
    different question from `entity_name`, an entity is named for lookup and unnamed for
    grading -- resolvable by a name no measurement can check.
    """
    profile = get_profile("typescript")
    source = b"const handler = () => {};\n"
    root = get_parser(profile.grammar).parse(source).root_node

    arrow = next(
        node
        for node in root.named_children[0].named_children[0].named_children
        if node.type == "arrow_function"
    )
    naming = profile.name_node(arrow)

    assert naming is not None
    assert source[naming.start_byte : naming.end_byte] == b"handler"
    assert profile.entity_name(arrow) == "handler"


def test_a_declared_function_is_unaffected() -> None:
    """The name field still wins where there is one, so nothing about ordinary declarations
    changes -- which is why four of the five corpora report identical numbers."""
    profile = get_profile("python")
    source = b"def declared():\n    pass\n"
    root = get_parser(profile.grammar).parse(source).root_node
    definition = root.named_children[0]

    assert profile.entity_name(definition) == "declared"
    assert profile.name_node(definition) == definition.child_by_field_name(profile.name_field)
