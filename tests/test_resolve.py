"""L0 lexical scoping and L1 project symbols."""

from __future__ import annotations

import pytest

from oxn.graph.model import Resolution
from oxn.languages import get_parser
from oxn.profiles import get_profile
from oxn.resolve.measure import symbol_tail
from oxn.resolve.scopes import build_scopes


def scopes(language: str, source: str):
    profile = get_profile(language)
    root = get_parser(language).parse(source.encode()).root_node
    return build_scopes(root, profile), source


def resolve(tree, source: str, name: str, near: str):
    """Resolve ``name`` at the position where ``near`` appears in the source."""
    return tree.resolve(name, source.index(near))


# ---- bindings ----------------------------------------------------------------------------


def test_module_level_names_are_bound() -> None:
    tree, _ = scopes("python", "TOP = 1\n\n\ndef f():\n    pass\n\n\nclass C:\n    pass\n")
    assert set(tree.root.bindings) == {"TOP", "f", "C"}


def test_parameters_bind_in_the_function_scope_not_the_module() -> None:
    tree, source = scopes("python", "def f(a, b=1, *c, **d):\n    return a\n")
    assert "a" not in tree.root.bindings
    assert resolve(tree, source, "a", "return a") is not None


def test_a_closure_sees_the_enclosing_scope() -> None:
    source = "def outer(a):\n    def inner(b):\n        return a + b\n    return inner\n"
    tree, _ = scopes("python", source)
    binding, scope = resolve(tree, source, "a", "return a + b")
    assert binding.kind == "parameter"
    assert scope.owner == "outer"


def test_shadowing_prefers_the_innermost_binding() -> None:
    source = "x = 1\n\n\ndef f():\n    x = 2\n    return x\n"
    tree, _ = scopes("python", source)
    _, scope = resolve(tree, source, "x", "return x")
    assert scope.kind == "function"


def test_comprehensions_get_their_own_scope() -> None:
    """`y` must not leak into the enclosing function, which is Python 3 semantics."""
    source = "def f(items):\n    squares = [y * y for y in items]\n    return squares\n"
    tree, _ = scopes("python", source)
    assert resolve(tree, source, "y", "return squares") is None


def test_global_statements_do_not_create_a_phantom_local() -> None:
    source = "count = 0\n\n\ndef bump():\n    global count\n    count = 1\n"
    tree, _ = scopes("python", source)
    _, scope = resolve(tree, source, "count", "count = 1")
    assert scope.kind == "module", "`global count` must resolve to the module binding"


# ---- receivers and class members ------------------------------------------------------------


def test_the_receiver_is_read_from_the_source_not_assumed_to_be_self() -> None:
    """Assuming `self` breaks every codebase spelling it otherwise, and LCOM depends on it."""
    tree, _ = scopes(
        "python",
        "class C:\n    def a(self, x):\n        pass\n\n    def b(myself, y):\n        pass\n",
    )
    receivers = {scope.owner: scope.receiver for scope in tree.all_scopes() if scope.receiver}
    assert receivers == {"a": "self", "b": "myself"}


def test_a_plain_function_has_no_receiver() -> None:
    tree, _ = scopes("python", "def free(a):\n    pass\n")
    assert all(scope.receiver is None for scope in tree.all_scopes())


def test_class_members_are_collected() -> None:
    tree, _ = scopes("python", "class C:\n    field = 1\n\n    def method(self):\n        pass\n")
    assert set(tree.class_members["C"]) == {"field", "method"}


def test_a_class_body_is_not_in_the_lexical_chain_of_its_methods() -> None:
    """`field` is not a bare name inside a method -- Python requires `self.field`."""
    source = "class C:\n    field = 1\n\n    def m(self):\n        return field\n"
    tree, _ = scopes("python", source)
    assert resolve(tree, source, "field", "return field") is None


# ---- imports -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import os\n", {"os"}),
        ("import numpy as np\n", {"np"}),
        ("import a.b.c\n", {"a"}),
        ("from a.b import thing\n", {"thing"}),
        ("from a.b import thing as t\n", {"t"}),
        ("from a.b import one, two as three\n", {"one", "three"}),
        ("from . import sibling\n", {"sibling"}),
    ],
)
def test_python_imports_bind_their_local_names(source: str, expected: set[str]) -> None:
    """`import numpy as np` puts `np` in scope and `numpy` nowhere at all."""
    tree, _ = scopes("python", source)
    assert set(tree.root.bindings) == expected


def test_import_aliases_record_where_a_name_came_from() -> None:
    tree, _ = scopes("python", "import numpy as np\nfrom a.b import thing as t\n")
    assert tree.import_aliases == {"np": "numpy", "t": "a.b"}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('import a from "./m";\n', {"a"}),
        ('import { b, c as d } from "./o";\n', {"b", "d"}),
        ('import * as ns from "pkg";\n', {"ns"}),
    ],
)
def test_typescript_imports_bind_their_local_names(source: str, expected: set[str]) -> None:
    tree, _ = scopes("typescript", source)
    assert expected <= set(tree.root.bindings)


# ---- L1 --------------------------------------------------------------------------------------


def project(sources: dict[str, str]):
    from oxn.graph.builder import build_file
    from oxn.resolve.symbols import build_project_symbols

    entities, trees = {}, {}
    profile = get_profile("python")
    for path, text in sources.items():
        data = text.encode()
        root = get_parser("python").parse(data).root_node
        entities[path] = list(build_file(path, data, profile, root).entities)
        trees[path] = build_scopes(root, profile)
    return build_project_symbols(entities, trees)


def test_a_local_declaration_wins() -> None:
    symbols = project({"a.py": "def helper():\n    pass\n", "b.py": "def helper():\n    pass\n"})
    found = symbols.resolve_call("a.py", "helper")
    assert found is not None
    assert found.file_path == "a.py"
    assert found.resolution is Resolution.L0


def test_a_unique_project_wide_name_resolves() -> None:
    symbols = project({"a.py": "def only_here():\n    pass\n", "b.py": "x = 1\n"})
    found = symbols.resolve_call("b.py", "only_here")
    assert found is not None
    assert found.is_certain
    assert found.resolution is Resolution.L1


def test_an_ambiguous_name_is_answered_with_low_confidence() -> None:
    """L1 always has an opinion, but says how much to trust it -- that is the gate's input."""
    symbols = project(
        {
            "a.py": "class A:\n    def get(self):\n        pass\n",
            "b.py": "class B:\n    def get(self):\n        pass\n",
            "c.py": "class C:\n    def get(self):\n        pass\n",
        }
    )
    found = symbols.resolve_call("c.py", "get")
    assert found is not None
    assert not found.is_certain
    assert found.confidence == pytest.approx(1 / 3)


def test_an_unknown_name_resolves_to_nothing() -> None:
    assert project({"a.py": "x = 1\n"}).resolve_call("a.py", "nope") is None


# ---- SCIP symbol parsing used by the measurement ------------------------------------------


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("scip-python python httpx 0.28 `httpx._client`/Client#get().", "get"),
        ("scip-python python httpx 0.28 `httpx._urls`/URLPattern#", "URLPattern"),
        ("scip-python python p 1 `pkg.base`/helper().", "helper"),
        ("scip-python python p 1 `pkg`/__init__:", "__init__"),
        ("local 12", "local 12"),
        # Rust. Every one of these came back wrong until 2026-09-06, and each was a
        # *silent exclusion* from the grader rather than a failure -- see `symbol_tail`.
        # A crate-level function has no `/` or `#` at all, so the whole symbol was the name:
        (
            "rust-analyzer cargo ripgrep 15.2.0 set_windows_exe_options().",
            "set_windows_exe_options",
        ),
        # a method carries the impl block that owns it, in brackets:
        (
            "rust-analyzer cargo grep-searcher 0.1.17 searcher/impl#[Searcher]search_reader().",
            "search_reader",
        ),
        # the owner is backtick-quoted when it is generic, and contains spaces and commas:
        (
            "rust-analyzer cargo grep-searcher 0.1.17 line_buffer/impl#"
            "[`LineBufferReader<'b, R>`]consume_all().",
            "consume_all",
        ),
        # and a trait impl carries two groups, trait and type:
        (
            "rust-analyzer cargo alloc 0.0.0 boxed/convert/impl#"
            "[`Box<dyn Error + 'a>`][`From<String>`]from().",
            "from",
        ),
        # Backtick quoting, which is the thing that makes a hand-written split wrong. SCIP
        # quotes a descriptor *exactly when* it contains a character that would otherwise be
        # structural, so a naive split cuts inside the one name that said it must not be.
        # ripgrep has this one for real -- a raw identifier holding a `#`:
        (
            "rust-analyzer cargo grep-cli 0.1.12 process/impl#[StderrReader]`r#async`().",
            "r#async",
        ),
        # and the same rule covers a `]` inside an impl owner, which ripgrep does not have
        # but any `impl From<[u8; 4]>` would:
        ("rust-analyzer cargo x 0.1 convert/impl#[X][`From<[u8; 4]>`]from().", "from"),
    ],
)
def test_symbol_tail(symbol: str, expected: str) -> None:
    assert symbol_tail(symbol) == expected


# ---- JSONC: the two traps that silently discard every path alias ------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # A comment marker inside a string is four ordinary bytes of a URL.
        (
            '{"paths": {"@x/*": ["http://example.com/*"]}}',
            '{"paths": {"@x/*": ["http://example.com/*"]}}',
        ),
        ('{"a": "// not a comment"}', '{"a": "// not a comment"}'),
        ('{"a": "/* nor this */"}', '{"a": "/* nor this */"}'),
        # A comment may follow a value on the same line.
        ('{"baseUrl": "./src", // where sources live\n"x": 2}', '{"baseUrl": "./src", \n"x": 2}'),
        ('{/* block */ "a": 1}', '{ "a": 1}'),
        # Trailing commas are legal in JSONC and fatal to json.loads.
        ('{"a": 1,}', '{"a": 1}'),
        ('{"a": [1, 2,],}', '{"a": [1, 2]}'),
        # An escape consumes the next character, so this quote does not close the string.
        ('{"a": "escaped \\" still // inside"}', '{"a": "escaped \\" still // inside"}'),
        # Unterminated constructs must not hang or raise.
        ('{"a": 1 /* unterminated', '{"a": 1 '),
        ('{"a": "unterminated', '{"a": "unterminated'),
    ],
)
def test_jsonc_stripping(source: str, expected: str) -> None:
    """`tsconfig.json` is JSONC, and getting this wrong loses every alias in the file."""
    from oxn.graph.manifests import _strip_jsonc

    assert _strip_jsonc(source) == expected


def test_a_stripped_tsconfig_still_parses_as_json() -> None:
    import json as _json

    from oxn.graph.manifests import _strip_jsonc

    source = (
        "{\n"
        "  // compiler options\n"
        '  "compilerOptions": {\n'
        '    "baseUrl": "./src", /* root */\n'
        '    "paths": { "@app/*": ["app/*"], },\n'
        "  },\n"
        "}\n"
    )
    parsed = _json.loads(_strip_jsonc(source))
    assert parsed["compilerOptions"]["paths"]["@app/*"] == ["app/*"]


# ---- ambiguity inside one file -----------------------------------------------------------

GO_RECEIVERS = """package metrics

type Counter struct{}
type Gauge struct{}

func (c *Counter) With(labels ...string) *Counter { return c }
func (g *Gauge) With(labels ...string) *Gauge { return g }

func use(c *Counter) { c.With("a") }
"""


def project_symbols(language: str, source: str, path: str):
    """A one-file project symbol table, built the way `measure_corpus` builds one."""
    from oxn.graph.builder import build_file
    from oxn.resolve.symbols import build_project_symbols

    profile = get_profile(language)
    data = source.encode()
    root = get_parser(language).parse(data).root_node
    parsed = build_file(path, data, profile, root)
    entities = list(parsed.entities)
    return build_project_symbols({path: entities}, {path: build_scopes(root, profile)})


def test_a_name_declared_twice_in_one_file_is_not_a_confident_answer() -> None:
    """Go breaks the assumption L1 was written under.

    A method there is a *top-level* declaration carrying its receiver in the signature
    rather than in a parent scope, so `Counter.With` and `Gauge.With` are two file-level
    `With`s. The table kept the first with `setdefault` and answered every call with it at
    confidence 1.0 -- and `metrics.callgraph` admits exactly the edges whose confidence is
    1.0 below L2, so a coin flip entered the call graph as ground truth. On `go-kit` this
    was 46 answers, and it took confident precision from 88.3% down to 83.9%.
    """
    symbols = project_symbols("go", GO_RECEIVERS, "metrics/metrics.go")

    answer = symbols.resolve_call("metrics/metrics.go", "With")

    assert answer is not None, "the name is still resolvable -- just not certainly"
    assert answer.confidence == 0.5, "two receivers, so one answer in two"
    assert not answer.is_certain


def test_an_unambiguous_file_local_name_is_still_certain() -> None:
    """The fix must not cost certainty where there was never any ambiguity, or every
    single-declaration call in Python and TypeScript stops entering the call graph."""
    symbols = project_symbols("go", GO_RECEIVERS, "metrics/metrics.go")

    answer = symbols.resolve_call("metrics/metrics.go", "use")

    assert answer is not None
    assert answer.is_certain


def test_declarations_in_omits_the_ambiguous_names() -> None:
    """`declarations_in` promises one entity per name. Where the file declares several, the
    honest answer is absence rather than whichever was parsed first."""
    symbols = project_symbols("go", GO_RECEIVERS, "metrics/metrics.go")

    declared = symbols.declarations_in("metrics/metrics.go")

    assert "With" not in declared
    assert "use" in declared


# ---- destructuring, and the require binding that was measured and not taken ----------------


REQUIRES = """const mutated = require("./m").thing;
const fs = require("fs");
const { helper, other: renamed } = require("./m");
const plain = compute();
const { destructured } = compute();

function use() {
  return helper() + renamed() + plain() + destructured() + fs.x();
}
"""


def test_a_shorthand_destructuring_binds_at_all() -> None:
    """`const { helper } = anything` yields `shorthand_property_identifier_pattern`, which is
    neither `identifier` nor a container of one -- so `_binding_targets` walked straight past
    the commonest destructuring in the language and bound nothing.

    Every consumer of the scope tree saw those names as undeclared, not only the resolver.
    Measured on `javascript-eslint`, fixing it moves no graded answer at all: 8,633 confident
    at 99.815% before and after. A binding the language makes and OXN did not is worth having
    right whether or not a number moves.
    """
    tree, text = scopes("javascript", REQUIRES)

    assert resolve(tree, text, "destructured", "return ") is not None
    assert resolve(tree, text, "helper", "return ") is not None
    assert resolve(tree, text, "renamed", "return ") is not None


def test_the_key_of_a_renaming_destructure_is_not_bound() -> None:
    """`const { other: renamed } = m` binds `renamed`. `other` is the exporter's name for it
    and names nothing here -- binding it would invent a local out of a property name."""
    tree, text = scopes("javascript", REQUIRES)

    assert resolve(tree, text, "other", "return ") is None


@pytest.mark.parametrize(("name", "source"), [("fs", "fs"), ("helper", "./m"), ("renamed", "./m")])
def test_a_commonjs_require_binds_the_import_it_is(name: str, source: str) -> None:
    """**`const fs = require("fs")` is an import written as an assignment.**

    The grammar has no `import_statement` to match, so every CommonJS binding was declared
    `local` -- and `scip.aliases` refuses a local as "not an import binding", correctly, given
    what it was told.

    This test asserted the *opposite* for two days. Binding it recovered 427 of eslint's 883
    refusals and made ten call sites confidently wrong, so it was held back with a null result
    pinned here. The ten turned out to be one call repeated, through a weakness in step 2 rather
    than in the binding: see `symbols._member_of`. With that fixed the same measurement reads
    +54 confident answers, 54 of them correct.
    """
    tree, text = scopes("javascript", REQUIRES)
    binding = resolve(tree, text, name, "return ")

    assert binding is not None, f"{name} binds nothing"
    assert binding[0].kind == "import"
    assert tree.import_aliases[name] == source


def test_an_ordinary_call_is_still_not_an_import() -> None:
    """The gate. `const plain = compute()` is a value, and answering it from the import table
    would reach whatever `plain` happens to name elsewhere in the project."""
    tree, text = scopes("javascript", REQUIRES)
    binding = resolve(tree, text, "plain", "return ")

    assert binding is not None and binding[0].kind == "local"
    assert "plain" not in tree.import_aliases


def test_a_require_binding_reassigned_later_is_flagged_as_assigned() -> None:
    """`scip.aliases` refuses a name that is also assigned, because the call may reach the
    assignment rather than the import. A CommonJS binding *is* an assignment, so counting its
    own declaration there would refuse every one -- `_bind_required` stays out of `assigned`,
    while a *second* assignment still lands there."""
    tree, _ = scopes("javascript", REQUIRES + "mutated = wrap(mutated);\n")

    assert "fs" not in tree.assigned, "the require declaration is not a rebinding of itself"
    assert "helper" not in tree.assigned
