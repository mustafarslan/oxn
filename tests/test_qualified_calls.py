"""Imports, and the calls that go through them.

Two halves of one subject. The first is the **import table**: the local name a file may
write, and where it came from -- recorded in all five launch languages, and in Go, Rust and
Java only since 2026-09-06, where `_imported_names` had been dispatching straight into the
ECMAScript reader and yielding nothing.

The second is what reads it. `resolve_call` is told what a call was written *through* -- the
`c` of `c.With()`, the `metrics` of `metrics.NewCounter()`, nothing at all for `helper()` --
and two rules follow, both derived from measured splits rather than assumed (ADR-0002, ninth
amendment):

* a **qualified** call is never answered by the calling file's own top-level declarations,
  which scored 0 for 265 in Rust, 0 for 51 on Go's package-qualified calls and 0 for 2 in
  Python, against 99.3%-100% for every later step;
* a call **governed by an import that reached no file in this tree** resolves to nothing,
  whether that governing name is a qualifier (`np.array()`) or the callee itself
  (`readFile()` after `import { readFile } from "fs"`).

Split out of `test_resolve.py` when that file crossed its 500-line ceiling. The seam is the
subject, not the size: nothing here is about lexical scoping, and nothing there is about
imports.
"""

from __future__ import annotations

import pytest

from oxn.languages import get_parser
from oxn.profiles import get_profile
from oxn.resolve.scopes import build_scopes


def scopes(language: str, source: str):
    profile = get_profile(language)
    root = get_parser(profile.grammar).parse(source.encode()).root_node
    return build_scopes(root, profile), source


def project(sources: dict[str, str]):
    """A multi-file Python project symbol table, with no dependency graph."""
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


def project_symbols(language: str, source: str, path: str):
    """A one-file project symbol table, built the way `measure_corpus` builds one."""
    from oxn.graph.builder import build_file
    from oxn.resolve.symbols import build_project_symbols

    profile = get_profile(language)
    data = source.encode()
    root = get_parser(language).parse(data).root_node
    entities = list(build_file(path, data, profile, root).entities)
    return build_project_symbols({path: entities}, {path: build_scopes(root, profile)})


# ---- import aliases, in every language that has them ---------------------------------------

IMPORTS = {
    "python": "import os\nimport numpy as np\nfrom x import y\n",
    "typescript": 'import fs from "fs";\nimport * as p from "path";\n',
    "go": 'package m\nimport (\n\t"fmt"\n\tex "e.com/t"\n)\n',
    "rust": "use std::fmt;\nuse a::b as c;\n",
    "java": "import java.util.List;\n",
}


@pytest.mark.parametrize("language", ["python", "typescript", "go", "rust", "java"])
def test_import_aliases_are_recorded(language: str) -> None:
    """L0 knows what a local name was imported as, in all five launch languages.

    Go, Rust and Java recorded nothing until 2026-09-06, and the cause was one line:
    `_imported_names` dispatched on `style != "python"` straight into the ECMAScript reader,
    which finds no ECMAScript import clause inside a `use_declaration` and yields nothing.
    The gap lived here as an inverted test until it was closed.
    """
    tree, _ = scopes(language, IMPORTS[language])

    assert tree.import_aliases, f"{language} recorded no import aliases"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # The alias, which is the whole point.
        ('package m\nimport ex "e.com/t"\n', {"ex": "e.com/t"}),
        # Unaliased: the local name comes from the path, by Go's convention.
        ('package m\nimport "fmt"\n', {"fmt": "fmt"}),
        ('package m\nimport "single/one"\n', {"one": "single/one"}),
        # Semantic import versioning: `/v2` is a *module* major version and never the
        # package name, so `casbin` is right and `v2` would be wrong. go-kit imports six
        # paths of this shape, which is how it was noticed.
        (
            'package m\nimport "github.com/casbin/casbin/v2"\n',
            {"casbin": "github.com/casbin/casbin/v2"},
        ),
        # gopkg.in spells the same convention with a dot.
        ('package m\nimport "gopkg.in/yaml.v2"\n', {"yaml": "gopkg.in/yaml.v2"}),
        # Grouped and single-line declarations are different tree shapes.
        ('package m\nimport (\n\t"fmt"\n\t"os"\n)\n', {"fmt": "fmt", "os": "os"}),
    ],
)
def test_go_import_shapes(source: str, expected: dict[str, str]) -> None:
    tree, _ = scopes("go", source)

    assert tree.import_aliases == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("use std::fmt;\n", {"fmt": "std"}),
        ("use a::b as c;\n", {"c": "a"}),
        ("use x::y::{p, q as r};\n", {"p": "x::y", "r": "x::y"}),
        # Lists nest, so the reader recurses rather than looking one level down.
        ("use deep::{a::{b, c as d}};\n", {"b": "deep::a", "d": "deep::a"}),
        # `self` is its own node kind in the grammar rather than an identifier, and it
        # re-binds the module itself. Treating it as an identifier drops this silently.
        ("use m::{self, n};\n", {"m": "m", "n": "m"}),
        ("use crate::x::y;\n", {"y": "crate::x"}),
        ("extern crate serde as sd;\n", {"sd": "serde"}),
    ],
)
def test_rust_use_shapes(source: str, expected: dict[str, str]) -> None:
    tree, _ = scopes("rust", source)

    assert tree.import_aliases == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import java.util.List;\n", {"List": "java.util"}),
        # A static import binds the member, and `java.lang.Math` is where it came from.
        ("import static java.lang.Math.max;\n", {"max": "java.lang.Math"}),
    ],
)
def test_java_import_shapes(source: str, expected: dict[str, str]) -> None:
    """Java has no import aliases at all, which is why this table is not really about
    aliases: it maps a simple name to where it came from, and that is what a resolver wants
    from any of the five."""
    tree, _ = scopes("java", source)

    assert tree.import_aliases == expected


@pytest.mark.parametrize(
    ("language", "source"),
    [
        # Imported for its side effects; it binds no name.
        ("go", 'package m\nimport _ "b.com/blank"\n'),
        # A dot import puts the package's members into file scope with *no qualifier*, so
        # this table has nothing to hold. That is a real binding and a different table, and
        # conflating them would claim a qualifier no one can write.
        ("go", 'package m\nimport . "d.com/dot"\n'),
        # A glob names nothing syntax can enumerate.
        ("rust", "use s::t::*;\n"),
        ("java", "import java.util.*;\n"),
    ],
)
def test_the_imports_that_bind_no_qualified_name(language: str, source: str) -> None:
    """**Inverted on purpose**, the way the whole-language gap used to be. These four are not
    unfinished work: each is an import whose local name syntax cannot supply, and recording a
    plausible-looking one would be worse than recording none."""
    tree, _ = scopes(language, source)

    assert not tree.import_aliases


# ---- the qualifier: what a call was written *through* --------------------------------------

QUALIFIER_CASES = [
    ("python", "pkg.thing()\n", "pkg"),
    ("python", "self.helper()\n", "self"),
    ("python", "a.b.c()\n", "a"),
    ("python", "plain()\n", None),
    ("go", "package m\nfunc f() { c.With() }\n", "c"),
    ("go", "package m\nfunc f() { plain() }\n", None),
    ("rust", "fn f() { r.consume_all(); }\n", "r"),
    # A path call is qualified too -- by a type or module rather than by a receiver. This
    # case asserted `None` for a day, on the reasoning that Rust tells the two apart by
    # shape; it does, and both of them are still qualified.
    ("rust", "fn f() { std::fmt::format(); }\n", "std"),
    ("rust", "fn f() { Searcher::new(); }\n", "Searcher"),
    ("typescript", "pkg.thing();\n", "pkg"),
    # Java keeps `object` and `name` as siblings of the *call*, so the callee is a bare
    # identifier and the qualifier hangs off the call node instead.
    ("java", "class A { void f() { Math.max(1, 2); } }", "Math"),
    ("java", "class A { void f() { this.g(); } }", "this"),
    ("java", "class A { void f() { g(); } }", None),
]


@pytest.mark.parametrize(("language", "source", "expected"), QUALIFIER_CASES)
def test_callee_qualifier(language: str, source: str, expected: str | None) -> None:
    """Both grammar shapes, read off the trees rather than assumed."""
    from oxn.resolve.measure import callee_qualifier

    profile = get_profile(language)
    spec = profile.metrics.cognitive
    root = get_parser(profile.grammar).parse(source.encode()).root_node

    found = []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type in spec.call_kinds:
            callee = node.child_by_field_name(spec.callee_field)
            if callee is not None:
                found.append(callee_qualifier(node, callee, profile))

    assert found == [expected]


def test_a_receiver_call_is_not_answered_by_the_calling_files_own_declarations() -> None:
    """The defect, in one assertion.

    `self.handle()` is a call on a receiver whose type L1 does not know. Answering it with
    the file's own top-level `handle` is not a weak guess, it is a category error -- and it
    was made at **confidence 1.0**, exactly the confidence `metrics.callgraph` admits.
    On the pinned corpora this step answered qualified calls 0/265 correctly in Rust,
    0/51 for Go's package-qualified calls and 0/2 in Python.
    """
    symbols = project(
        {
            "a.py": "def handle():\n    pass\n\n\ndef go(self):\n    self.handle()\n",
            "b.py": "def handle():\n    pass\n",
        }
    )

    bare = symbols.resolve_call("a.py", "handle")
    qualified = symbols.resolve_call("a.py", "handle", "self")

    assert bare is not None and bare.is_certain and bare.file_path == "a.py"
    assert qualified is not None, "it still answers -- it just stops being sure"
    assert not qualified.is_certain, "two candidates, and locality is no longer a tiebreak"


def test_a_bare_call_still_prefers_the_calling_file() -> None:
    """The rule is about *qualified* calls only. Locality is still the best evidence there
    is for `helper()` written on its own, and 100% precise on every corpus measured."""
    symbols = project({"a.py": "def helper():\n    pass\n", "b.py": "def helper():\n    pass\n"})

    found = symbols.resolve_call("a.py", "helper")

    assert found is not None
    assert found.file_path == "a.py"
    assert found.is_certain


def test_a_python_method_call_does_not_take_the_modules_own_function() -> None:
    """Python is covered by the same rule and needed it: a module holding a top-level
    `handle` answered `self.handle()` with it, certainly. Only two sites on httpx -- because
    a Python method lives in a class and so is not in the top-level table to begin with --
    and both were wrong."""
    symbols = project(
        {
            "m.py": "def handle():\n    pass\n\n\nclass C:\n"
            "    def go(self):\n        self.handle()\n",
            "other.py": "def handle():\n    pass\n",
        }
    )

    assert symbols.resolve_call("m.py", "handle").is_certain, "the bare call is unchanged"
    assert not symbols.resolve_call("m.py", "handle", "self").is_certain


def qualified_project(sources: dict[str, str], root):
    """A project symbol table built *with* its dependency graph, which is what supplies the
    "did this import reach anything" half."""
    from oxn.graph.builder import build_file
    from oxn.graph.depgraph import build_dependency_graph
    from oxn.resolve.symbols import build_project_symbols

    profile = get_profile("python")
    entities, trees = {}, {}
    for name, text in sources.items():
        (root / name).write_text(text)
    for name, text in sources.items():
        data = text.encode()
        node = get_parser("python").parse(data).root_node
        entities[name] = list(build_file(name, data, profile, node).entities)
        trees[name] = build_scopes(node, profile)
    graph = build_dependency_graph(root, [root / name for name in sources])
    return build_project_symbols(entities, trees, graph)


def test_a_call_through_an_import_that_left_the_tree_resolves_to_nothing(tmp_path) -> None:
    """`np` names `numpy`, `numpy` is not in this tree, so `np.array()` is not an entity
    here -- and any in-tree `array` is a coincidence.

    **The grader cannot see this class at all**: the oracle places these callees out of
    tree, so they are never graded. They reach `metrics.callgraph`, CBO and RFC, where L1
    answered 233 of them confidently on go-kit and 16 on httpx.
    """
    symbols = qualified_project(
        {"app.py": "import numpy as np\n\n\ndef array():\n    pass\n"}, tmp_path
    )

    assert symbols.resolve_call("app.py", "array") is not None, "the bare call still answers"
    assert symbols.resolve_call("app.py", "array", "np") is None


def test_an_import_that_reached_a_file_in_the_tree_is_not_external(tmp_path) -> None:
    """The other half: only a *placed* specifier that reached nothing counts. An import OXN
    resolved is ordinary, and its qualifier must not silence the lookup."""
    symbols = qualified_project(
        {
            "lib.py": "def array():\n    pass\n",
            "app.py": "import lib\n\n\ndef unrelated():\n    pass\n",
        },
        tmp_path,
    )

    found = symbols.resolve_call("app.py", "array", "lib")

    assert found is not None
    assert found.file_path == "lib.py"


def test_an_alias_the_graph_never_placed_does_not_count_as_external(tmp_path) -> None:
    """Absence of evidence. Rust records `use std::fmt` as `fmt -> std` while the import
    specifier is `std.fmt`: the two never join, so no entry is found. Declining to fire is
    the only correct behaviour -- treating a missing key as "external" would declare most of
    two languages unresolvable."""
    symbols = project_symbols("rust", "use std::fmt;\n\nfn format() {}\n", "m.rs")

    assert symbols.external_aliases.get("m.rs", frozenset()) == frozenset()
    assert symbols.resolve_call("m.rs", "format", "fmt") is not None


def test_a_chain_headed_by_a_call_is_qualified_by_a_value_not_a_module(tmp_path) -> None:
    """`verify(repo).findByLastName()` -- Mockito. `verify` is an external static import, but
    the mock it returns has an **in-tree** type and the method really is ours.

    Asking the import table about `verify` declared that call external and cost exactly two
    graded answers on petclinic. A call in the chain means the qualifier is a value, so the
    module rule must not fire -- while the call is still qualified, so the *locality* rule
    still does.
    """
    from oxn.resolve.measure import callee_qualifier

    profile = get_profile("java")
    source = b"class T { void t() { verify(repo).findByLastName(); a.b.c(); } }"
    root = get_parser("java").parse(source).root_node

    found = {}
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type in profile.metrics.cognitive.call_kinds:
            callee = node.child_by_field_name(profile.metrics.cognitive.callee_field)
            if callee is not None:
                found[callee.text.decode()] = callee_qualifier(node, callee, profile)

    assert found["findByLastName"] == "verify(repo)", "not `verify`: it is not a module"
    assert found["c"] == "a", "a pure name chain still reports its head"
    assert found["verify"] is None


def test_a_bare_call_to_a_name_imported_from_outside_the_tree_resolves_to_nothing(
    tmp_path,
) -> None:
    """`import { readFile } from "fs"` binds `readFile`, so a bare `readFile()` *is* that
    binding -- and an in-tree function of the same name is a coincidence, not the callee.

    The qualified form of this rule shipped first and this one did not. Like its qualified
    sibling the grader cannot score these: the oracle places the callee out of tree, so the
    site is never graded, and the wrong answer reaches `metrics.callgraph`, CBO and RFC
    instead. This rule withdraws **35** of them across the corpora -- 27 on nest, 7 on httpx,
    1 on go-kit -- and changes no graded number anywhere.

    **35, not the 519 that motivated the search**, and the difference is the finding. "The
    oracle placed this callee out of tree" is not the same claim as "this call leaves the
    tree", and counting the first as if it were the second overstated the defect by an order
    of magnitude. On nest the remaining 619 are at least three unrelated things: 391 are
    names imported from `@nestjs/*` packages that *do* resolve in-tree, so nothing about them
    is external; 101 are not imported at all; and the sampled causes are a **parameter**
    (`callback`, which OXN has no entity for), a `const` arrow function (`sleep`), and a
    genuine TypeScript lib global (`fetch` from `lib.dom.d.ts`). Only the last is this rule's
    business. The rest are callees OXN has no entity for, which is a different gap and wants
    its own measurement rather than this one's.
    """
    symbols = qualified_project(
        {
            "app.py": "from numpy import array\n\n\ndef unrelated():\n    pass\n",
            "lib.py": "def array():\n    pass\n",
        },
        tmp_path,
    )

    assert symbols.resolve_call("app.py", "array") is None
    assert symbols.resolve_call("lib.py", "array") is not None, "lib declares it; lib means it"


def test_a_local_declaration_beats_an_import_of_the_same_name(tmp_path) -> None:
    """The shadowing case, which is legal Python and which the rule must not break.

    `from external import helper` followed by `def helper()` leaves `helper` bound to the
    *local* definition -- the language says the later binding wins. Locality is consulted
    before the import table for exactly this reason, so the rule never has to know about it.
    """
    symbols = qualified_project(
        {"app.py": "from numpy import helper\n\n\ndef helper():\n    pass\n"}, tmp_path
    )

    found = symbols.resolve_call("app.py", "helper")

    assert found is not None
    assert found.file_path == "app.py"
    assert found.is_certain


def test_a_qualified_call_is_still_governed_by_its_qualifier_not_its_name(tmp_path) -> None:
    """`np.array()` is governed by `np`, and `array()` by `array`. Collapsing the two would
    make a local declaration of the callee silence a package call that has nothing to do
    with it."""
    symbols = qualified_project(
        {"app.py": "import numpy as np\n\n\ndef array():\n    pass\n"}, tmp_path
    )

    assert symbols.resolve_call("app.py", "array") is not None, "the file declares it"
    assert symbols.resolve_call("app.py", "array", "np") is None
