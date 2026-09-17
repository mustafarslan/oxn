"""Every launch grammar must load and parse clean source without error nodes.

This is the operational verification of the ``tree-sitter-language-pack`` dependency
(ADR-0001): the package advertises 371 grammars fetched lazily, and this test proves the
six OXN actually launches with work on the running interpreter. It also de-risks P1 --
the ``LanguageProfile`` work assumes these parsers exist and behave.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oxn.languages import LAUNCH_LANGUAGES, get_parser
from oxn.profiles import profile_for_path

#: Minimal, unambiguous source per language, exercising a function and a branch -- the two
#: constructs every Tier-1 metric is built on.
SNIPPETS: dict[str, str] = {
    "python": "def f(a):\n    if a > 0:\n        return a\n    return -a\n",
    "typescript": "function f(a: number): number {\n  if (a > 0) { return a; }\n  return -a;\n}\n",
    "javascript": "function f(a) {\n  if (a > 0) { return a; }\n  return -a;\n}\n",
    "go": "package main\n\nfunc f(a int) int {\n\tif a > 0 {\n\t\treturn a\n\t}\n\treturn -a\n}\n",
    "rust": "fn f(a: i32) -> i32 {\n    if a > 0 { return a; }\n    -a\n}\n",
    "java": "class C {\n  int f(int a) {\n    if (a > 0) { return a; }\n    return -a;\n  }\n}\n",
}


@pytest.mark.parametrize("language", sorted(LAUNCH_LANGUAGES))
def test_grammar_parses_clean_source(language: str) -> None:
    parser = get_parser(language)
    tree = parser.parse(SNIPPETS[language].encode())

    # ``has_error`` covers ERROR and MISSING anywhere in the subtree. docs/metrics.md
    # section 9.4 makes this a hard rule: a tree with error nodes suppresses the affected
    # metrics rather than emitting a silently wrong number.
    assert tree.root_node.type not in {"ERROR", ""}
    assert not tree.root_node.has_error, f"{language} grammar produced error nodes"
    assert tree.root_node.end_byte == len(SNIPPETS[language].encode())


@pytest.mark.parametrize("language", sorted(LAUNCH_LANGUAGES))
def test_grammar_finds_a_function(language: str) -> None:
    """Sanity check that the tree has real structure, not one flat node."""
    parser = get_parser(language)
    tree = parser.parse(SNIPPETS[language].encode())

    kinds = set()
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        kinds.add(node.type)
        stack.extend(node.children)

    assert any("function" in k or "method" in k or "declaration" in k for k in kinds), (
        f"{language}: no function-like node found among {sorted(kinds)}"
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("a/b/c.py", "python"),
        ("src/index.ts", "typescript"),
        ("src/index.tsx", "typescript"),
        ("src/index.mts", "typescript"),
        ("src/index.js", "javascript"),
        ("src/index.mjs", "javascript"),
        ("src/index.cjs", "javascript"),
        ("main.go", "go"),
        ("lib.rs", "rust"),
        ("Main.java", "java"),
        ("README.md", None),
        ("noextension", None),
    ],
)
def test_language_detection(path: str, expected: str | None) -> None:
    """Asked of the *profile* registry, which is what the walk reads.

    `oxn.languages` carried a second extension table that nothing in `src/` used and this
    test did: it was missing `.cjs`, `.mts` and `.cts`, so a public mapping disagreed with
    the one that decides which files get analysed. One table, and it is the one with a
    consumer.
    """
    found = profile_for_path(path)
    assert (found.name if found is not None else None) == expected


#: A React component: a `.tsx` file whose body is JSX. The plain `typescript` grammar cannot
#: parse this at all -- JSX is the whole reason `tree-sitter-typescript` ships two grammars.
TSX_SOURCE = (
    "export const Card = ({ title }: { title: string }) => {\n"
    '  return <div className="card">{title}</div>;\n'
    "};\n"
)


def test_a_tsx_file_is_parsed_by_the_tsx_grammar() -> None:
    """`.tsx` reached the `typescript` grammar until 2026-09-17, and JSX does not parse there.

    The consequence was silent, which is what made it survive: every pass drops a file whose
    tree `has_error` -- duplication, the import graph, the symbol index, resolution -- and
    the gate reported the file as checked and clean. A React project got a green light and an
    import graph with its components missing.
    """
    profile = profile_for_path("Card.tsx")
    assert profile is not None
    assert profile.grammar == "tsx"
    # The label stays `typescript`: it is what every metric, cache row and report groups by,
    # and a phantom "tsx" language would split TypeScript's figures in half.
    assert profile.name == "typescript"

    tree = get_parser(profile.grammar).parse(TSX_SOURCE.encode())
    assert not tree.root_node.has_error


def test_the_typescript_grammar_really_cannot_do_it() -> None:
    """The guard against a future 'simplification' that points `.tsx` back at `typescript`.

    Without this, the fix above looks like a preference between two interchangeable grammars.
    It is not one.
    """
    assert get_parser("typescript").parse(TSX_SOURCE.encode()).root_node.has_error


def test_plain_typescript_files_did_not_move() -> None:
    """`.ts`, `.mts` and `.cts` keep the grammar they had; only `.tsx` changed."""
    for suffix in (".ts", ".mts", ".cts"):
        profile = profile_for_path(f"a{suffix}")
        assert profile is not None
        assert profile.grammar == "typescript"


def test_tsx_differs_from_typescript_in_exactly_two_fields() -> None:
    """A profile *is* a grammar's node-kind vocabulary, so the two must not drift.

    `TSX` is built with `dataclasses.replace`, which makes drift impossible today; this fails
    if someone later writes it out by hand, which is how two copies of a table start agreeing
    only until one of them is edited.
    """
    from dataclasses import fields

    from oxn.profiles.typescript import TSX, TYPESCRIPT

    differing = {
        field.name
        for field in fields(TYPESCRIPT)
        if getattr(TYPESCRIPT, field.name) != getattr(TSX, field.name)
    }
    assert differing == {"grammar", "extensions"}


def test_the_by_name_registry_still_has_one_entry_per_language() -> None:
    """`PROFILES` is keyed by `profile.name`, which TSX shares with TYPESCRIPT deliberately.

    Adding TSX there would overwrite the entry every by-name lookup returns, silently giving
    `.ts` files the TSX grammar -- the same class of bug in the other direction.
    """
    from oxn.profiles import PROFILES, get_profile
    from oxn.profiles.typescript import TYPESCRIPT

    assert set(PROFILES) == set(LAUNCH_LANGUAGES)
    assert get_profile("typescript") is TYPESCRIPT


def test_a_tsx_file_survives_the_real_pipeline(tmp_path: Path) -> None:
    """The grammar has to reach the *parse sites*, which is where the bug actually lived.

    `LanguageProfile.grammar` existed from the start and was read by nothing: every site
    called `get_parser(profile.name)`, which agreed with the grammar for all six launch
    languages and so was never wrong until `.tsx` needed them to differ. A test that only
    checks the profile passes with every one of those sites reverted, so this runs a `.tsx`
    file through `Indexer.index` and asserts the tree came back whole.
    """
    from oxn.graph.indexer import Indexer

    (tmp_path / "Card.tsx").write_text(TSX_SOURCE)
    (tmp_path / "plain.ts").write_text(SNIPPETS["typescript"])

    with Indexer(root=tmp_path, cache_path=tmp_path / "graph.db") as indexer:
        report = indexer.index([tmp_path])

    assert report.parsed == 2
    assert report.incomplete == [], f"{report.incomplete} did not parse"


def test_an_import_inside_a_tsx_file_reaches_the_dependency_graph(tmp_path: Path) -> None:
    """The consequence the user would actually see: components missing from the import graph.

    A file that fails to parse is dropped by `depgraph`, `scan`, `project` and `measure`
    without a word, so the visible symptom was not an error -- it was a React project whose
    components had no edges.
    """
    from oxn.graph.depgraph import build_dependency_graph
    from oxn.graph.indexer import Indexer

    (tmp_path / "Card.tsx").write_text('import { helper } from "./helper";\n' + TSX_SOURCE)
    (tmp_path / "helper.ts").write_text("export const helper = 1;\n")

    with Indexer(root=tmp_path, cache_path=tmp_path / "graph.db") as indexer:
        graph = build_dependency_graph(tmp_path, list(indexer.sources([tmp_path])))

    assert graph.files.get("Card.tsx") == {"helper.ts"}
