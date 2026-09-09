"""One class, five languages, and the numbers that must agree.

Cohesion is a property of a design, not of a syntax. The same two-collaborator service --
a `db` half and a `cache` half, four methods, no cross-talk -- is written here in Python,
Java, TypeScript, Go and Rust, and every LCOM variant must read the same on all five. It is
the cheapest possible check and it was not being made, so four of the five were wrong:

===========  ===============  ==============  ==========================================
language     LCOM3/4 before   LCOM3/4 after   what was broken
===========  ===============  ==============  ==========================================
Python       2                2               nothing
Java         4                2               no receiver at all, so `this.db` matched
                                              nothing and every method was a singleton
TypeScript   4                2               `class_body` opens a second, *nameless*
                                              class scope, which blanked the class name
                                              and left every method receiverless
Go           --               --              methods are top-level with a receiver and
                                              are never joined to their struct
Rust         --               --              methods live in `impl` blocks, which are
                                              never joined to the type they implement
===========  ===============  ==============  ==========================================

Java and TypeScript reported LCOM\\* 1.25 -- above 1, which is OXN's own signal for "the
fields are never touched" (`test_tier3.py`) -- and stamped the answer EXACT. Go and Rust
found the type but attached no methods to it, so every class read NOM 0.

The Go and Rust halves are xfail rather than deleted: the defect is real, the fixtures are
correct, and a passing xfail is the signal that the join has landed.
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

WORKING = ("python", "java", "typescript")
#: Their methods are declared outside the type -- Go by receiver, Rust by `impl` block --
#: and `build_class_models` looks only at a class body.
NOT_YET_JOINED = ("go", "rust")

_SUFFIX = {"python": "py", "java": "java", "typescript": "ts", "go": "go", "rust": "rs"}


def _cohesion(language: str):
    """Every LCOM variant for the one `Service` class in that language's fixture."""
    from oxn.languages import get_parser
    from oxn.metrics.cohesion import cohesion
    from oxn.profiles import get_profile
    from oxn.resolve.members import build_class_models
    from oxn.resolve.scopes import build_scopes

    profile = get_profile(language)
    data = (FIXTURES / f"service.{_SUFFIX[language]}.txt").read_bytes()
    tree = get_parser(language).parse(data)
    assert not tree.root_node.has_error, f"the {language} fixture does not parse"
    models = build_class_models(tree.root_node, profile, build_scopes(tree.root_node, profile))
    assert "Service" in models, f"{language}: no class named Service was found at all"
    return cohesion(models["Service"])


@pytest.mark.parametrize("language", WORKING)
def test_the_same_design_measures_the_same_in_every_language(language: str) -> None:
    """The whole point. A number that moves with the syntax is not measuring the design."""
    measured = _cohesion(language)
    assert measured.lcom3 == EXPECTED_COMPONENTS, f"{language}: lcom3 {measured.lcom3}"
    assert measured.lcom4 == EXPECTED_COMPONENTS, f"{language}: lcom4 {measured.lcom4}"
    assert measured.lcom1 == EXPECTED_LCOM1, f"{language}: lcom1 {measured.lcom1}"
    assert measured.lcom_star == pytest.approx(EXPECTED_LCOM_STAR), f"{language}: lcom*"


@pytest.mark.parametrize("language", WORKING)
def test_the_fields_are_actually_reached(language: str) -> None:
    """The specific regression, stated as the thing that was false rather than as a total.

    `LCOM* > 1` is what OXN reports when no method touches any field, and it is what Java
    and TypeScript reported for every class in the corpus while claiming EXACT.
    """
    measured = _cohesion(language)
    assert measured.lcom_star is not None and measured.lcom_star <= 1.0, (
        f"{language}: lcom* {measured.lcom_star} -- the fields are being missed again"
    )


@pytest.mark.xfail(reason="methods are declared outside the type and are not joined to it")
@pytest.mark.parametrize("language", NOT_YET_JOINED)
def test_go_and_rust_methods_reach_their_type(language: str) -> None:
    """Known broken, and stated rather than skipped, so a fix announces itself."""
    assert _cohesion(language).lcom4 == EXPECTED_COMPONENTS
