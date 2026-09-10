"""`shredding` fired in two of six launch languages, and `oxn init` told every user otherwise.

P10 shipped this rule early because the gap was found being *exploited* rather than
predicted: a 28-point function split into eight dedicated helpers passed with exit 0 while
`oxn init` was writing into every user's `CLAUDE.md` that exactly this "is detected and
rejected as shredding". The rule closed that -- in Python.

Measured 2026-09-10, one 14-helper shred transliterated across all six:

===========  ==================  ==================================================
language     `shredding` fired   what caught it instead
===========  ==================  ==================================================
Python                     yes   --
Go                         yes   --
Java                        no   the class aggregates, because the fixture had a class
TypeScript                  no   the class aggregates, same reason
JavaScript                  no   the class aggregates, same reason
Rust                        no   **nothing**
===========  ==================  ==================================================

Written as module-level free functions instead, TypeScript and JavaScript escaped too. Three
languages with a completely free shred, and the same sentence in `CLAUDE.md` for all six.

The cause was that privacy was a question about a *name*. Python spells it with an
underscore and Go with casing; the other four use modifiers, which is a question about a
node -- so `profiles.base.declared_private` answers it where the node is, and `Unit` carries
the answer to the rule.

**One case still declines, on purpose.** A CommonJS file publishes through
`module.exports.x = x`, which no `export_statement` records, so "not exported" proves
nothing there and the rule does not fire. That is most of the JavaScript ecosystem and it is
named rather than papered over -- `common.js` is the control that holds it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "shred_languages"

#: 14 helpers of cognitive 1 plus a root that calls them all: a cluster total of 14 against
#: the cognitive ceiling of 12. Every one is private by that language's own rule.
SHREDS = ("shred.py", "shred.go", "shred.rs", "Router.java", "shred.ts", "shred.js")

#: The same shape with public helpers. "Called once in this file" does not prove a public
#: name has no callers elsewhere, so the rule must decline rather than guess -- including for
#: CommonJS, where a module can publish anything by assignment.
PUBLIC = ("public.rs", "Public.java", "public.ts", "common.js")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _rules(project: Path, name: str) -> dict[str, float]:
    from oxn.check import run_check

    (project / name).write_text((FIXTURES / f"{name}.txt").read_text())
    report = run_check([name], use_baseline=False)
    return {finding.rule: finding.value for finding in report.blocking}


@pytest.mark.parametrize("name", SHREDS)
def test_a_shred_is_caught_in_every_launch_language(project: Path, name: str) -> None:
    found = _rules(project, name)
    assert "shredding" in found, f"{name}: {sorted(found)}"
    assert found["shredding"] == 14, f"{name}: counted {found['shredding']}"


@pytest.mark.parametrize("name", PUBLIC)
def test_public_helpers_are_not_provably_dedicated(project: Path, name: str) -> None:
    """Inverted on purpose: the rule declining is the correct answer, not a miss."""
    assert "shredding" not in _rules(project, name), name


def test_privacy_is_read_from_the_node_where_the_name_cannot_say_it() -> None:
    """The unit behind the table, asserted per language rather than through the gate."""
    from oxn.languages import get_parser
    from oxn.profiles import get_profile
    from oxn.profiles.base import declared_private

    cases = [
        ("rust", "fn hidden(a: i32) -> i32 { a }\n", "hidden", True),
        ("rust", "pub fn shown(a: i32) -> i32 { a }\n", "shown", False),
        ("java", "class A { private int a(int x) { return x; } }\n", "a", True),
        ("java", "class A { public int b(int x) { return x; } }\n", "b", False),
    ]
    for language, source, name, expected in cases:
        profile = get_profile(language)
        root = get_parser(profile.grammar).parse(source.encode()).root_node
        node = next(
            found
            for found in _walk(root)
            if found.type in profile.function_like and profile.entity_name(found) == name
        )
        assert declared_private(node, name, profile, esm=False) is expected, (language, name)


def _walk(node):
    stack = [node]
    while stack:
        found = stack.pop()
        stack.extend(found.named_children)
        yield found
