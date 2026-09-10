"""A class whose methods are written as fields, and the two ceilings that stopped seeing them.

`handler = (x) => x` is how a modern TypeScript or JavaScript class writes a method that
keeps `this`. The grammar spells it as a field holding an arrow function rather than as a
`method_definition`, and the builder classified it by node kind alone -- so it became an
anonymous lambda, `_class_totals` counts only methods, and a class made entirely of them read
**NOM 0 / WMC 0.0**. Both aggregates are in `GATED_METRICS`, so that is a live evasion of the
gate and not only a wrong report: thirteen methods over a ceiling of twelve pass by being
rewritten in the idiom most of the ecosystem already uses.

**Java is the control that decides it.** `Runnable a = () -> {}` is the same shape and Java
counted it all along -- because a Java lambda is not in the builder's anonymous set, so it
fell through to "inside a type, therefore a method". One shape, two answers, and the smaller
one is what a ceiling was compared against. Python's `a = lambda self, x: ...` was the third
answer: named, but a lambda, and so uncounted.

The fix is the discriminator, not the kind list: a callable declared in a class body **and
bound to a name** is a method. Both halves are load-bearing. Without the name, `xs = [lambda:
1]` in a class body becomes a method nothing can call. Without the class body, every
`const inner = () => {}` inside a function body becomes one too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oxn.thresholds import MAX_METHODS_PER_CLASS

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "class_fields"

#: One class of thirteen single-branch members per language, written twice: once as methods,
#: once as fields holding a callable. Same work, same count, and the gate must not be able to
#: tell them apart.
PAIRS = (
    ("javascript", "methods.js", "fields.js"),
    ("typescript", "methods.ts", "fields.ts"),
    ("python", "methods.py", "fields.py"),
    ("java", "Methods.java", "Fields.java"),
)

#: Thirteen members, one over the ceiling; each is a ternary, so cyclomatic 2 apiece.
EXPECTED_NOM = 13
EXPECTED_WMC = 26.0


def _aggregates(name: str) -> tuple[int, float]:
    """(NOM, WMC) of the one class in a fixture, straight off the metric engine."""
    from oxn.graph.builder import build_file
    from oxn.languages import get_parser
    from oxn.metrics.engine import measure_file
    from oxn.profiles import get_profile

    language = {".js": "javascript", ".ts": "typescript", ".py": "python", ".java": "java"}[
        Path(name).suffix
    ]
    profile = get_profile(language)
    data = (FIXTURES / f"{name}.txt").read_bytes()
    root = get_parser(profile.grammar).parse(data).root_node
    parsed = build_file(name, data, profile, root)
    for measured in measure_file(list(parsed.entities), data, profile, root):
        if measured.kind.value == "class":
            return int(measured.get("nom")), float(measured.get("wmc"))
    raise AssertionError(f"{name} declares no class")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _check(project: Path, name: str) -> tuple[int, set[str]]:
    from oxn.check import run_check

    (project / name).write_text((FIXTURES / f"{name}.txt").read_text())
    report = run_check([name], use_baseline=False)
    return report.exit_code, {finding.rule for finding in report.blocking}


@pytest.mark.parametrize(("language", "methods", "fields"), PAIRS)
def test_a_class_counts_the_same_however_its_methods_are_written(
    language: str, methods: str, fields: str
) -> None:
    """The control pair, held to a number rather than to equality alone.

    Equality on its own is satisfied by 0 == 0, which is exactly the state this closes.
    """
    assert _aggregates(methods) == (EXPECTED_NOM, EXPECTED_WMC), language
    assert _aggregates(fields) == (EXPECTED_NOM, EXPECTED_WMC), language


@pytest.mark.parametrize(("language", "methods", "fields"), PAIRS)
def test_rewriting_methods_as_fields_does_not_walk_through_the_ceiling(
    project: Path, language: str, methods: str, fields: str
) -> None:
    """The evasion itself, at the surface an agent actually meets."""
    assert EXPECTED_NOM > MAX_METHODS_PER_CLASS, "the fixture must exceed the ceiling to test it"
    for variant in (methods, fields):
        exit_code, rules = _check(project, variant)
        assert exit_code != 0, f"{language}/{variant}: thirteen methods passed the gate"
        assert "methods_per_class" in rules, f"{language}/{variant}: {sorted(rules)}"


# ---- the discriminator, both halves ------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "source", "expected"),
    [
        # Named and in a class body: a method, whatever the grammar calls the node.
        ("javascript", "class A { h = (x) => x; }\n", [("class", "A"), ("method", "h")]),
        ("typescript", "class A { h = (x: number) => x; }\n", [("class", "A"), ("method", "h")]),
        ("python", "class A:\n    h = lambda self, x: x\n", [("class", "A"), ("method", "h")]),
        # In a class body with no name a caller could write: still a lambda.
        ("python", "class A:\n    xs = [lambda: 1]\n", [("class", "A"), ("lambda", None)]),
        # Named but not in a class body: still a lambda. `inside_type` is False inside a
        # method's own body, which is what keeps every local closure out of the method count.
        (
            "javascript",
            "class A { m() { const inner = () => 1; return inner(); } }\n",
            [("class", "A"), ("method", "m"), ("lambda", "inner")],
        ),
    ],
)
def test_a_method_is_a_named_callable_declared_in_a_class(
    language: str, source: str, expected: list[tuple[str, str | None]]
) -> None:
    from tests.test_bound_callables import named

    assert named(language, source) == expected


# ---- and the other path that counts methods ----------------------------------------------


def _model(name: str):
    """The class as `oxn classes` builds it: the member model, not the metric engine."""
    from oxn.languages import get_parser
    from oxn.profiles import get_profile, profile_for_path
    from oxn.resolve.members import build_class_models
    from oxn.resolve.scopes import build_scopes

    profile = profile_for_path(name)
    assert profile is not None, name
    profile = get_profile(profile.name)
    data = (FIXTURES / f"{name}.txt").read_bytes()
    root = get_parser(profile.grammar).parse(data).root_node
    return build_class_models(root, profile, build_scopes(root, profile))["Panel"]


@pytest.mark.parametrize(("language", "methods", "fields"), PAIRS)
def test_both_paths_that_count_methods_give_the_same_answer(
    language: str, methods: str, fields: str
) -> None:
    """The standing lesson of `tests/test_impl_block_evasion.py`, applied to this shape.

    The gate reads `metrics.engine`; `oxn classes`, LCOM, RFC and CBO read the member model.
    Fixing only the first left `oxn classes` reporting WMC 0 and LCOM4 0 for the same
    thirteen-method class the gate had just started rejecting -- and when one question has
    two answers, a ceiling is compared against the smaller.
    """
    assert len(_model(methods).methods) == EXPECTED_NOM, language
    assert len(_model(fields).methods) == EXPECTED_NOM, language


#: A class whose class-body member is itself a class. The member model descends through a
#: member to find the callable it declares, and a nested class is where that descent must
#: stop -- Java's builder and Python's `class Config` are the everyday shapes.
NESTED = (
    (
        "java",
        "Outer.java",
        "class Outer { static class Builder { Outer build() { return 1; } } }\n",
    ),
    ("python", "m.py", "class Outer:\n    class Config:\n        def validate(self): pass\n"),
    ("typescript", "m.ts", "class Outer { static Inner = class { m() { return 1; } }; }\n"),
)


@pytest.mark.parametrize(("language", "path", "source"), NESTED)
def test_an_inner_class_keeps_its_own_methods(language: str, path: str, source: str) -> None:
    """The regression the pair above could not see, because no fixture nests.

    Descending through a member to reach `handler = (x) => x` also walked into a nested
    class and returned its first method as the outer class's. The gate reads containment and
    counts it under the inner class, so this was the same two-answer split in the other
    direction -- and `Outer` would have carried a method it does not have into LCOM.
    """
    from oxn.languages import get_parser
    from oxn.profiles import get_profile
    from oxn.resolve.members import build_class_models
    from oxn.resolve.scopes import build_scopes

    profile = get_profile(language)
    data = source.encode()
    root = get_parser(profile.grammar).parse(data).root_node
    models = build_class_models(root, profile, build_scopes(root, profile))

    assert models["Outer"].methods == {}, f"{language}: the outer class took an inner method"
    inner = next(model for name, model in models.items() if name != "Outer")
    assert len(inner.methods) == 1, language


@pytest.mark.parametrize(("language", "_methods", "fields"), PAIRS)
def test_a_method_written_as_a_field_is_not_also_a_field(
    language: str, _methods: str, fields: str
) -> None:
    """Otherwise LCOM reads every one of them as an attribute no method touches."""
    model = _model(fields)
    assert model.fields & set(model.methods) == set(), language
