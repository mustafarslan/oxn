r"""One class, five languages, and the numbers that must agree.

Cohesion is a property of a design, not of a syntax. The same two-collaborator service --
a `db` half and a `cache` half, four methods, no cross-talk -- is written here in Python,
Java, TypeScript, Go and Rust, and every LCOM variant must read the same on all five. It is
the cheapest possible check and it was not being made, so four of the five were wrong:

===========  ======  ====================================================================
language     before  what was broken
===========  ======  ====================================================================
Python            2  nothing
Java              4  no receiver at all, so `this.db` matched nothing and every method was
                     its own component
TypeScript        4  `class_body` opens a second, *nameless* class scope, which blanked
                     the enclosing class name and left every method receiverless
Go            NOM 0  methods are top-level declarations carrying a receiver, and were
                     never joined to the struct they belong to
Rust          NOM 0  methods live in `impl` blocks, which carry no name of their own and
                     were never joined to the type they implement
===========  ======  ====================================================================

All five now read 2. Java and TypeScript had been reporting LCOM\* 1.25 -- above 1, which is
OXN's own signal for "the fields are never touched" (`test_tier3.py`) -- and stamping the
answer EXACT. Go and Rust found the type, attached no methods to it, and a class with no
methods reports a perfectly cohesive 0, so nothing looked wrong.

LCOM\* is asserted only where the fixtures share a denominator. Go and Rust have no
constructor, so they carry four methods against the others' five, and Henderson-Sellers
normalises by that count: 0.667 against 0.5 is the same design, correctly reported.

`statics` covers the other half of the receiver question -- the callables that sit in a class
and have no receiver at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cohesion_shapes"

#: The design every fixture encodes: 4 methods over 2 fields that never meet, so the class
#: splits cleanly in two. Constructors are excluded from the scan, so they are not counted.
EXPECTED_COMPONENTS = 2
EXPECTED_LCOM1 = 4
EXPECTED_LCOM_STAR = 0.5

ALL = ("python", "java", "typescript", "go", "rust")
#: The three whose fixtures carry a constructor, so LCOM* shares a denominator.
WITH_CONSTRUCTOR = ("python", "java", "typescript")

_SUFFIX = {"python": "py", "java": "java", "typescript": "ts", "go": "go", "rust": "rs"}


def _model(language: str, stem: str = "service"):
    """The `ClassModel` for the one class in that language's fixture."""
    from oxn.languages import get_parser
    from oxn.profiles import get_profile
    from oxn.resolve.members import build_class_models
    from oxn.resolve.scopes import build_scopes

    profile = get_profile(language)
    data = (FIXTURES / f"{stem}.{_SUFFIX[language]}.txt").read_bytes()
    tree = get_parser(language).parse(data)
    assert not tree.root_node.has_error, f"the {language} {stem} fixture does not parse"
    return build_class_models(tree.root_node, profile, build_scopes(tree.root_node, profile))


def _cohesion(language: str):
    """Every LCOM variant for the one `Service` class in that language's fixture."""
    from oxn.metrics.cohesion import cohesion

    models = _model(language)
    assert "Service" in models, f"{language}: no class named Service was found at all"
    return cohesion(models["Service"])


@pytest.mark.parametrize("language", ALL)
def test_the_same_design_measures_the_same_in_every_language(language: str) -> None:
    """The whole point. A number that moves with the syntax is not measuring the design."""
    measured = _cohesion(language)
    assert measured.lcom3 == EXPECTED_COMPONENTS, f"{language}: lcom3 {measured.lcom3}"
    assert measured.lcom4 == EXPECTED_COMPONENTS, f"{language}: lcom4 {measured.lcom4}"
    assert measured.lcom1 == EXPECTED_LCOM1, f"{language}: lcom1 {measured.lcom1}"


@pytest.mark.parametrize("language", ALL)
def test_the_fields_are_actually_reached(language: str) -> None:
    """The specific regression, stated as the thing that was false rather than as a total.

    `LCOM* > 1` is what OXN reports when no method touches any field, and it is what Java
    and TypeScript reported for every class in the corpus while claiming EXACT.
    """
    measured = _cohesion(language)
    assert measured.lcom_star is not None and measured.lcom_star <= 1.0, (
        f"{language}: lcom* {measured.lcom_star} -- the fields are being missed again"
    )


@pytest.mark.parametrize("language", ALL)
def test_every_method_reaches_its_type(language: str) -> None:
    """Go declares methods beside the struct and Rust inside `impl` blocks. Both must arrive.

    Stated as a count rather than through LCOM, because a class with no methods reports a
    perfectly cohesive 0 -- which is how these two read as fine while measuring nothing.
    """
    measured = _cohesion(language)
    expected = 5 if language in WITH_CONSTRUCTOR else 4
    assert measured.method_count == expected, f"{language}: {measured.method_count} methods"


@pytest.mark.parametrize("language", WITH_CONSTRUCTOR)
def test_lcom_star_agrees_where_the_method_count_does(language: str) -> None:
    """The normalised variant, held only across fixtures sharing a denominator."""
    assert _cohesion(language).lcom_star == pytest.approx(EXPECTED_LCOM_STAR)


def test_a_python_static_method_has_no_receiver() -> None:
    """`@staticmethod` sits in a class and takes no receiver, and the binder must agree.

    Without the exclusion, parameter zero of `parse(raw)` was bound as the receiver and
    every `raw.strip()` was recorded as a field access on the class. `@classmethod` is the
    control: it *does* take one, spelled `cls`, and must keep it.
    """
    holder = _model("python", "statics")["Holder"]
    assert holder.methods["parse"].receiver == "", "a static method has no receiver"
    assert holder.methods["parse"].touches == set(), "`raw` is a parameter, not a field"
    assert holder.methods["of"].receiver == "cls", "a class method's receiver is the class"
    assert holder.methods["save"].receiver == "self"


def test_a_rust_associated_function_is_not_a_method() -> None:
    """`fn new(cfg: Config)` and `fn save(&self)` sit in one `impl`, and only one is a method.

    The `self_parameter` node is the only thing that separates them. Reading parameter zero
    instead would have made `cfg` the receiver, and every `cfg.x` a field of `Holder`.
    """
    holder = _model("rust", "statics")["Holder"]
    assert holder.methods["new"].receiver == "", "an associated function has no receiver"
    assert holder.methods["save"].receiver == "self"
    assert holder.methods["save"].touches == {"db"}


def test_a_go_method_binds_the_receiver_it_declares() -> None:
    """Go writes the receiver outside the parameter list, so it reaches no parameter scan."""
    service = _model("go")["Service"]
    assert {access.receiver for access in service.methods.values()} == {"s"}
    assert service.methods["Save"].touches == {"db"}
