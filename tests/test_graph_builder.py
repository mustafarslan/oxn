"""The containment skeleton.

Two rules established here are inherited by every later phase, so they are tested for their
own sake rather than through a metric:

* a wrapped definition is one entity, spanning the wrapper;
* a nested definition is its *own* entity and is not folded into its parent -- which is
  exactly the "do not descend into nested functions" rule cyclomatic complexity needs.
"""

from __future__ import annotations

from oxn.graph.model import EntityKind, entity_id


def qnames(parsed, kind: EntityKind | None = None) -> list[str]:
    return [e.qualified_name for e in parsed.entities if kind is None or e.kind is kind]


def by_name(parsed, qualified_name: str):
    return next(e for e in parsed.entities if e.qualified_name == qualified_name)


# ---- identity -----------------------------------------------------------------------


def test_entity_ids_are_position_independent() -> None:
    """A function keeps its id when code above it moves, so trends survive edits."""
    first = entity_id("a.py", EntityKind.FUNCTION, "a.f")
    again = entity_id("a.py", EntityKind.FUNCTION, "a.f")
    sibling = entity_id("a.py", EntityKind.FUNCTION, "a.f", occurrence=1)
    assert first == again
    assert first != sibling


def test_same_named_siblings_get_distinct_ids(build) -> None:
    parsed = build("python", "def f(): pass\ndef f(): pass\n")
    functions = [e for e in parsed.entities if e.kind is EntityKind.FUNCTION]
    assert len(functions) == 2
    assert functions[0].id != functions[1].id


# ---- structure ----------------------------------------------------------------------


def test_module_is_the_root_and_the_only_orphan(build) -> None:
    parsed = build("python", "def f(): pass\n")
    roots = [e for e in parsed.entities if e.parent_id is None]
    assert len(roots) == 1
    assert roots[0].kind is EntityKind.MODULE
    assert parsed.root is roots[0]


def test_nested_functions_are_separate_entities(build) -> None:
    parsed = build("python", "def outer():\n    def inner():\n        pass\n    return inner\n")
    assert qnames(parsed, EntityKind.FUNCTION) == ["pkg.sample.outer", "pkg.sample.outer.inner"]
    assert (
        by_name(parsed, "pkg.sample.outer.inner").parent_id
        == by_name(parsed, "pkg.sample.outer").id
    )


def test_decorator_lines_belong_to_the_decorated_entity(build) -> None:
    parsed = build("python", "@one\n@two\ndef f():\n    pass\n")
    fn = by_name(parsed, "pkg.sample.f")
    assert fn.start_line == 1, "the entity must span its decorators"
    assert fn.attrs["wrapped_by"] == "decorated_definition"


def test_methods_are_distinguished_from_functions(build) -> None:
    parsed = build("python", "class C:\n    def m(self):\n        pass\n\ndef g():\n    pass\n")
    assert qnames(parsed, EntityKind.METHOD) == ["pkg.sample.C.m"]
    assert qnames(parsed, EntityKind.FUNCTION) == ["pkg.sample.g"]


def test_function_nested_in_a_method_is_a_function_not_a_method(build) -> None:
    parsed = build(
        "python", "class C:\n    def m(self):\n        def helper():\n            pass\n"
    )
    assert qnames(parsed, EntityKind.METHOD) == ["pkg.sample.C.m"]
    assert qnames(parsed, EntityKind.FUNCTION) == ["pkg.sample.C.m.helper"]


def test_lambdas_are_captured_with_a_positional_name(build) -> None:
    parsed = build("python", "f = lambda x: x\n")
    lambdas = [e for e in parsed.entities if e.kind is EntityKind.LAMBDA]
    assert len(lambdas) == 1
    assert lambdas[0].attrs["parameters"] == ["x"]


def test_parameters_are_recorded(build) -> None:
    parsed = build("python", "def f(a, b=1, *c, **d):\n    pass\n")
    assert by_name(parsed, "pkg.sample.f").attrs["parameters"] == ["a", "b", "c", "d"]


# ---- TypeScript ---------------------------------------------------------------------


def test_typescript_exported_class_is_one_entity(build) -> None:
    parsed = build("typescript", "export class K {\n  m(a: number) { return a; }\n}\n")
    assert qnames(parsed, EntityKind.CLASS) == ["pkg.sample.K"]
    assert qnames(parsed, EntityKind.METHOD) == ["pkg.sample.K.m"]
    assert by_name(parsed, "pkg.sample.K").attrs["wrapped_by"] == "export_statement"


def test_typescript_interfaces_are_interfaces_not_classes(build) -> None:
    parsed = build("typescript", "interface I { m(): void }\nclass C {}\n")
    assert qnames(parsed, EntityKind.INTERFACE) == ["pkg.sample.I"]
    assert qnames(parsed, EntityKind.CLASS) == ["pkg.sample.C"]


def test_typescript_abstract_class_is_marked_abstract(build) -> None:
    parsed = build("typescript", "abstract class A {}\nclass B {}\n")
    assert by_name(parsed, "pkg.sample.A").attrs.get("is_abstract") is True
    assert "is_abstract" not in by_name(parsed, "pkg.sample.B").attrs


def test_arrow_functions_are_lambdas(build) -> None:
    parsed = build("typescript", "const f = (q: number) => q + 1;\n")
    assert [e.kind for e in parsed.entities if e.kind is EntityKind.LAMBDA]


# ---- error handling -----------------------------------------------------------------


def test_broken_source_is_flagged_not_dropped(build) -> None:
    """A tree with ERROR nodes must be recorded as incomplete, not silently trusted."""
    parsed = build("python", "def f(:\n    pass\n")
    assert parsed.parse_incomplete is True


def test_clean_source_is_not_flagged(build) -> None:
    assert build("python", "def f():\n    pass\n").parse_incomplete is False
