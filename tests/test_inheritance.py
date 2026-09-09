"""DIT and NOC, which worked in Python and in no other language.

The CK suite's inheritance half read zero for TypeScript, JavaScript, Java and Rust, because
`_base_names` looked in fields only Python has. Every class in four of six languages reported
no supertypes, so DIT was 0 and NOC was 0 -- and neither looked broken, because 0 is what a
root class reports and most classes are roots.

    language     Leaf DIT     Base NOC
                 was  now     was  now
    Python         2    2       1    1
    TypeScript     0    2       0    1
    JavaScript     0    2       0    1
    Java           0    2       0    1
    Rust           0    1       0    2

Rust differs on purpose: it has no inheritance chain, so `impl Base for Mid` makes `Mid` a
direct implementor and `Base` has two of them. Go is absent from the table for the same kind
of reason -- its interfaces are satisfied structurally and never declared, so there is no
edge to find and reporting one would be an invention.

Two narrower defects came out of the same read. A generic parameter was counted as a
supertype, so `class Foo[T](Base)` reported bases `("Base", "T")`, adding a level to DIT and a
class to CBO. And the hierarchy was keyed by bare name across the whole project, so a
directory holding `m.go` and `m.rs` gave Go's `Base` interface two children that were Rust's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "inheritance"

_FILE = {
    "python": "m.py",
    "typescript": "m.ts",
    "javascript": "m.js",
    "java": "M.java",
    "rust": "m.rs",
}

#: (language, Leaf's DIT, Base's NOC). Rust implements rather than extends, so its `Base` has
#: two direct implementors and neither is deeper than one.
EXPECTED = (
    ("python", 2, 1),
    ("typescript", 2, 1),
    ("javascript", 2, 1),
    ("java", 2, 1),
    ("rust", 1, 2),
)


def _models(language: str, source: str | None = None):
    from oxn.languages import get_parser
    from oxn.profiles import get_profile
    from oxn.resolve.members import build_class_models
    from oxn.resolve.scopes import build_scopes

    profile = get_profile(language)
    data = source.encode() if source else (FIXTURES / f"{_FILE[language]}.txt").read_bytes()
    tree = get_parser(language).parse(data)
    assert not tree.root_node.has_error, f"the {language} fixture does not parse"
    return build_class_models(tree.root_node, profile, build_scopes(tree.root_node, profile))


@pytest.mark.parametrize(("language", "leaf_dit", "base_noc"), EXPECTED)
def test_inheritance_is_read_in_every_language_that_declares_it(
    language: str, leaf_dit: int, base_noc: int
) -> None:
    from oxn.metrics.coupling import build_hierarchy

    models = _models(language)
    hierarchy = build_hierarchy({_FILE[language]: models})
    assert hierarchy.depth(language, "Leaf")[0] == leaf_dit, f"{language}: Leaf"
    assert hierarchy.child_count(language, "Base") == base_noc, f"{language}: Base"


def test_go_declares_no_inheritance_and_none_is_invented() -> None:
    """Go's interfaces are satisfied structurally. An edge here would be a fabrication."""
    from oxn.metrics.coupling import build_hierarchy

    models = _models("go", (FIXTURES / "m.go.txt").read_text())
    hierarchy = build_hierarchy({"m.go": models})
    assert all(not model.bases for model in models.values()), models
    assert hierarchy.child_count("go", "Base") == 0


@pytest.mark.parametrize(
    ("language", "source", "expected"),
    (
        ("python", "class Foo[T](Base):\n    pass\n", ("Base",)),
        ("typescript", "class A extends Box<Inner> { go() { return 1; } }", ("Box",)),
    ),
)
def test_a_generic_parameter_is_not_a_supertype(
    language: str, source: str, expected: tuple[str, ...]
) -> None:
    """`type_parameters` was read as a supertype field, which is a different thing entirely.

    The cost was not cosmetic: a phantom base adds a level to DIT and a class to CBO, and
    both are reported as measurements.
    """
    models = _models(language, source)
    assert [model.bases for model in models.values()] == [expected]


def test_two_languages_in_one_directory_do_not_share_a_hierarchy() -> None:
    """A bare supertype name means one thing inside a language and nothing across two.

    Go's `Base` is an interface no Go code declares an implementation of. Keyed by name
    alone, it collected Rust's two implementors -- a `noc` no Go program can produce.
    """
    from oxn.metrics.coupling import build_hierarchy

    hierarchy = build_hierarchy(
        {"m.go": _models("go", (FIXTURES / "m.go.txt").read_text()), "m.rs": _models("rust")}
    )
    assert hierarchy.child_count("go", "Base") == 0
    assert hierarchy.child_count("rust", "Base") == 2
