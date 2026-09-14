"""Grading L0/L1 against a SCIP oracle, without needing an indexer installed.

`test_oracles.py` measures this on real SCIP output, and that test is the authority --
but it needs `scip-python` on PATH and is skipped wherever that is absent, which is
everywhere by default. So the grading logic itself had no running coverage at all: a
refactor of it could not be verified by any test that actually executes.

These build a ScipDocument by hand instead. Small, exact, and they run everywhere.
"""

from __future__ import annotations

from oxn.graph.builder import build_file
from oxn.languages import get_parser
from oxn.profiles import get_profile
from oxn.resolve.measure import ResolutionAccuracy, _Grading, grade_file
from oxn.resolve.scopes import build_scopes
from oxn.resolve.symbols import build_project_symbols
from oxn.scip.index import ScipDocument, ScipOccurrence

SOURCE = """
def helper(value):
    return value


def caller(value):
    return helper(value)
"""


def _graded(symbol: str, *, line: int, char: int, truth_is_helper: bool = True):
    """Grade `SOURCE`'s single call site against one hand-made SCIP occurrence."""
    profile = get_profile("python")
    data = SOURCE.encode()
    tree = get_parser(profile.grammar).parse(data)
    parsed = build_file("m.py", data, profile, tree.root_node)
    entities = list(parsed.entities)

    symbols = build_project_symbols(
        {"m.py": entities}, {"m.py": build_scopes(tree.root_node, profile)}
    )
    helper = next(e for e in entities if e.name == "helper")
    document = ScipDocument(
        relative_path="m.py",
        occurrences=[ScipOccurrence(symbol, line, char, line, char + 6, 0)],
    )
    accuracy = ResolutionAccuracy(language="python")
    grade_file(
        _Grading(
            "m.py",
            profile,
            document,
            symbols,
            {symbol: helper.id if truth_is_helper else "someone-else"},
            accuracy,
        ),
        tree.root_node,
    )
    return accuracy


#: `helper(value)` sits on line 7 (0-based 6), starting at column 11.
CALL_LINE, CALL_CHAR = 6, 11


def test_a_correct_guess_is_counted_correct() -> None:
    accuracy = _graded("`m`/helper().", line=CALL_LINE, char=CALL_CHAR)
    assert accuracy.graded_call_sites == 1
    assert accuracy.answered == 1
    assert accuracy.correct == 1
    assert not accuracy.disagreements


def test_a_wrong_guess_is_recorded_with_its_evidence() -> None:
    """A disagreement must name the file, line and both answers, or it cannot be chased."""
    accuracy = _graded("`m`/helper().", line=CALL_LINE, char=CALL_CHAR, truth_is_helper=False)
    assert accuracy.answered == 1
    assert accuracy.correct == 0
    assert len(accuracy.disagreements) == 1
    assert "m.py:7" in accuracy.disagreements[0]
    assert "helper()" in accuracy.disagreements[0]


def test_an_oracle_that_contradicts_the_source_is_excluded_not_graded() -> None:
    """30.5% of httpx call sites hit this: scip-python names an adjacent symbol.

    Grading against a ground truth that is wrong one time in five measures the oracle
    rather than us, so those sites are excluded and counted separately.
    """
    accuracy = _graded("`m`/somethingelse().", line=CALL_LINE, char=CALL_CHAR)
    assert accuracy.excluded_untrustworthy == 1
    assert accuracy.graded_call_sites == 0


def test_a_call_site_scip_did_not_place_is_skipped_silently() -> None:
    """No occurrence at that position means there is nothing to grade against."""
    accuracy = _graded("`m`/helper().", line=99, char=0)
    assert accuracy.graded_call_sites == 0
    assert accuracy.excluded_untrustworthy == 0
    assert not accuracy.disagreements


def test_step_two_answers_from_the_file_the_name_came_from() -> None:
    """**"Any file this one imports" is a guess dressed as evidence.**

    Step 2 asked whether *any* imported file declares the name and took the first in sorted
    order -- so a file importing two modules that both declare `helper` answered confidently
    from whichever sorted first. The import table already knows better: `aliases` records which
    specifier bound the name and `specifier_targets` records where that specifier was placed.

    On `javascript-eslint` this fires for 1,950 of 1,965 step-2 answers, so it is the common
    path rather than a corner.

    **Measured on all six languages, and Go is why it is worth having.** Python, Rust,
    TypeScript, JavaScript and Java are identical with it and without -- same graded count,
    same confident count, same precision. Go is not: confident precision **99.116% -> 99.411%**
    with the confident count unchanged at 1,018, so it converts wrong answers into right ones
    rather than adding answers. That is package-qualified dispatch, `metrics.NewCounter()`,
    where "any file this one imports" and "the file `metrics` was imported from" are different
    files and only the second is evidence. An earlier claim here said it moved no published
    number; that was measured on eslint alone, before Go's indexer was installed.
    """
    from oxn.graph.model import Entity, EntityKind
    from oxn.resolve.symbols import ProjectSymbols

    def declared(name: str, path: str) -> Entity:
        return Entity(
            id=f"{path}::{name}",
            kind=EntityKind.FUNCTION,
            name=name,
            qualified_name=f"{path}.{name}",
            file_path=path,
            start_byte=0,
            end_byte=1,
            start_line=1,
            end_line=1,
        )

    symbols = ProjectSymbols()
    symbols.by_file = {
        "a/first.js": {"helper": [declared("helper", "a/first.js")]},
        "b/second.js": {"helper": [declared("helper", "b/second.js")]},
    }
    symbols.imports = {"caller.js": {"a/first.js", "b/second.js"}}
    symbols.aliases = {"caller.js": {"helper": "./b/second"}}
    symbols.specifier_targets = {"caller.js": {"./b/second": ("b/second.js",)}}

    found = symbols.resolve_call("caller.js", "helper")

    assert found is not None
    assert found.file_path == "b/second.js", "the file the name was imported from, not the first"
    assert found.confidence == 1.0


def test_a_name_published_by_a_barrel_resolves_to_where_it_is_declared() -> None:
    """**A barrel declares nothing, and stopping at it turns an answer into a guess.**

    `import { Injectable } from "@nestjs/common"` places the specifier at
    `packages/common/index.ts`, which is `export * from "./decorators"` and forty lines like
    it. Step 2 searched that file, found no `Injectable`, and fell through to "a unique
    declaration anywhere in the project" -- which is the rung with no locality argument behind
    it. `typescript-nest` has 96 such files and 451 re-export edges, nested two deep.

    Measured on nest: confident answers **1,402 -> 1,410 and correct 1,402 -> 1,410**, so all
    eight are right and precision stays at 100%. The other five corpora do not move, which is
    what a change aimed at one language's idiom should look like.
    """
    from oxn.graph.model import Entity, EntityKind
    from oxn.resolve.symbols import ProjectSymbols

    def declared(name: str, path: str) -> Entity:
        return Entity(
            id=f"{path}::{name}",
            kind=EntityKind.FUNCTION,
            name=name,
            qualified_name=f"{path}.{name}",
            file_path=path,
            start_byte=0,
            end_byte=1,
            start_line=1,
            end_line=1,
        )

    symbols = ProjectSymbols()
    symbols.by_file = {
        "pkg/decorators/injectable.ts": {
            "Injectable": [declared("Injectable", "pkg/decorators/injectable.ts")]
        }
    }
    symbols.imports = {"app.ts": {"pkg/index.ts"}}
    symbols.aliases = {"app.ts": {"Injectable": "./pkg"}}
    symbols.specifier_targets = {"app.ts": {"./pkg": ("pkg/index.ts",)}}
    symbols.reexports = {
        "pkg/index.ts": ("pkg/decorators/index.ts",),
        "pkg/decorators/index.ts": ("pkg/decorators/injectable.ts",),
    }

    found = symbols.resolve_call("app.ts", "Injectable")

    assert found is not None, "two hops through barrels is the common case, not a corner"
    assert found.file_path == "pkg/decorators/injectable.ts"
    assert found.confidence == 1.0


def test_a_barrel_that_declares_the_name_itself_wins_over_one_it_republishes() -> None:
    """Breadth first, so the nearer declaration answers. A barrel that both declares and
    re-publishes is rare and the language's own answer is the local one."""
    from oxn.graph.model import Entity, EntityKind
    from oxn.resolve.symbols import ProjectSymbols

    def declared(name: str, path: str) -> Entity:
        return Entity(
            id=f"{path}::{name}",
            kind=EntityKind.FUNCTION,
            name=name,
            qualified_name=f"{path}.{name}",
            file_path=path,
            start_byte=0,
            end_byte=1,
            start_line=1,
            end_line=1,
        )

    symbols = ProjectSymbols()
    symbols.by_file = {
        "pkg/index.ts": {"helper": [declared("helper", "pkg/index.ts")]},
        "pkg/deep.ts": {"helper": [declared("helper", "pkg/deep.ts")]},
    }
    symbols.aliases = {"app.ts": {"helper": "./pkg"}}
    symbols.specifier_targets = {"app.ts": {"./pkg": ("pkg/index.ts",)}}
    symbols.reexports = {"pkg/index.ts": ("pkg/deep.ts",)}

    assert symbols.resolve_call("app.ts", "helper").file_path == "pkg/index.ts"


def test_barrels_that_re_export_each_other_do_not_loop() -> None:
    """`seen`, because a package's `index.ts` files routinely point at one another."""
    from oxn.resolve.symbols import ProjectSymbols

    symbols = ProjectSymbols()
    symbols.aliases = {"app.ts": {"missing": "./a"}}
    symbols.specifier_targets = {"app.ts": {"./a": ("a.ts",)}}
    symbols.reexports = {"a.ts": ("b.ts",), "b.ts": ("a.ts",)}

    assert symbols.resolve_call("app.ts", "missing") is None


def test_a_class_qualified_call_resolves_against_the_class_not_the_file() -> None:
    """**A named import is usually a class, and step 2 treated every one as a namespace.**

    `_from_imports` asks whether the file imported anything under the qualifier and, if so,
    resolves the name against the imported file's *top-level* declarations. That is right for
    `metrics.NewCounter()`, where `metrics` is a package. It is wrong for
    `Config.getRuleOptionsSchema()`, where `Config` is a class -- and `lib/config/config.js` in
    eslint declares **both** a free `getRuleOptionsSchema` and a static
    `Config.getRuleOptionsSchema`, so the bare-name lookup answered the wrong one confidently,
    ten times.

    Two functions with one name in one file is exactly the case a bare-name lookup cannot
    decide and a qualified one can. Measured: this is what made the CommonJS `require` binding
    safe to ship, turning +10 confident answers with 0 correct into +54 with 54 correct.
    """
    from oxn.graph.model import Entity, EntityKind
    from oxn.resolve.symbols import ProjectSymbols

    def declared(name: str, qualified: str, path: str, kind: EntityKind) -> Entity:
        return Entity(
            id=qualified,
            kind=kind,
            name=name,
            qualified_name=qualified,
            file_path=path,
            start_byte=0,
            end_byte=1,
            start_line=1,
            end_line=1,
        )

    free = declared("schema", "config.schema", "config.js", EntityKind.FUNCTION)
    method = declared("schema", "config.Config.schema", "config.js", EntityKind.METHOD)

    symbols = ProjectSymbols()
    symbols.by_file = {"config.js": {"schema": [free]}}  # only file-level declarations
    symbols.by_name["schema"] = [free, method]  # every entity, which is where the member is
    symbols.aliases = {"app.js": {"Config": "./config"}}
    symbols.specifier_targets = {"app.js": {"./config": ("config.js",)}}

    through_class = symbols.resolve_call("app.js", "schema", "Config")
    assert through_class is not None
    assert through_class.entity_id == "config.Config.schema", "the class's, not the file's"
    assert through_class.confidence == 1.0

    # A package-qualified call has no such member and still gets the file-level declaration,
    # which is what step 2 was built for.
    symbols.aliases = {"app.js": {"config": "./config"}}
    symbols.specifier_targets = {"app.js": {"./config": ("config.js",)}}
    through_package = symbols.resolve_call("app.js", "schema", "config")
    assert through_package is not None
    assert through_package.entity_id == "config.schema"
