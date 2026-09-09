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


def test_java_reaches_a_field_written_without_this() -> None:
    """Spring style writes `vets.findAll()`, and reading only `this.vets` measured nothing.

    On petclinic's `VetControllerTests` this moved LCOM* from 1.125 -- above 1, the "no
    method touches any field" signature -- to 0.875, and LCOM4 from 5 to 2.

    Two controls sit in the fixture, and both are shadowing rather than syntax. The
    constructor's `this.db = db` has a *parameter* named `db` on its right-hand side, and
    `shadowed()` declares a local of the same name: neither may count as a field access, and
    only an L0 lookup can tell. A text match would have scored both.
    """
    service = _model("java", "bare_fields")["Service"]
    assert service.methods["save"].touches == {"db"}, "a bare field read must be found"
    assert service.methods["evict"].touches == {"cache"}
    assert service.methods["shadowed"].touches == set(), "a local shadows the field"
    assert service.methods["delegates"].calls == {"save"}, "a bare sibling call joins LCOM4"


def test_java_bare_access_does_not_invent_cohesion() -> None:
    """The fixture still splits in two, so the new reads are the right reads.

    Three components, not two: `delegates` calls `save` and joins the `db` half, while
    `shadowed` touches nothing this class owns and is correctly alone. Over-matching would
    show up as *fewer* components than that, by inventing a field the two halves share.
    """
    from oxn.metrics.cohesion import cohesion

    measured = cohesion(_model("java", "bare_fields")["Service"])
    assert measured.lcom4 == 3, f"lcom4 {measured.lcom4}"


@pytest.mark.parametrize(
    ("language", "source"),
    (
        ("python", "def run():\n    f = lambda x: x * 2\n    return f(3)\n"),
        (
            "go",
            "package p\n\nfunc Run() {\n\tf := func(x int) int { return x * 2 }\n\t_ = f(3)\n}\n",
        ),
        ("rust", "pub fn run() {\n    let f = |x: i32| x * 2;\n    let _ = f(3);\n}\n"),
        ("typescript", "function run() {\n  const f = (x: number) => x * 2;\n  return f(3);\n}\n"),
    ),
)
def test_an_anonymous_callable_is_labelled_one(language: str, source: str) -> None:
    """Go's `func_literal` and Rust's `closure_expression` read as ordinary functions.

    Gating did not change -- a lambda is a callable kind and was always measured -- but the
    label was false, and Rust's took the name of whatever it was bound to, which put
    `let f = |x| ...` into the *named* function population as `f`. Any consumer selecting on
    kind saw two of five languages wrongly.
    """
    from oxn.graph.builder import build_file
    from oxn.languages import get_parser
    from oxn.profiles import get_profile

    profile = get_profile(language)
    data = source.encode()
    tree = get_parser(language).parse(data)
    assert not tree.root_node.has_error, f"the {language} snippet does not parse"
    kinds = {
        entity.kind.value
        for entity in build_file("m", data, profile, tree.root_node).entities
        if entity.kind.value != "module"
    }
    assert kinds == {"function", "lambda"}, f"{language}: {sorted(kinds)}"
