"""The launch language set and how to load its grammars.

A language is *supported* when it has a full ``LanguageProfile`` (grammar id, node-kind
mappings, tree-sitter queries) -- that arrives in P1. This module is the layer beneath
that: the mapping from an OXN language name to a tree-sitter grammar, and the loader.

Grammars come from ``tree-sitter-language-pack`` (MIT, 371 grammars, each parser fetched
and cached on first use so the install stays small). See ADR-0001.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from tree_sitter import Language, Parser

#: OXN language name -> tree-sitter-language-pack grammar id.
LAUNCH_LANGUAGES: dict[str, str] = {
    "python": "python",
    "typescript": "typescript",
    "javascript": "javascript",
    "go": "go",
    "rust": "rust",
    "java": "java",
}


#: Every grammar OXN parses with. **Not the same set as the launch languages**, and that is
#: the point: TypeScript needs two, because `tree-sitter-typescript` ships `typescript` and
#: `tsx` as separate grammars and the first cannot parse JSX at all.
#:
#: `.tsx` was mapped to the `typescript` grammar until 2026-09-17, so every React component
#: in a repository failed to parse -- and failed *silently*, because each pass drops a file
#: with `has_error` and nothing reported it: no import edges, no duplication, no symbols, no
#: call graph, and a clean bill of health. `LanguageProfile.grammar` existed for exactly this
#: distinction and was read by nothing; every parse site took `profile.name` instead, which
#: worked only because the two agreed for all six launch languages.
GRAMMARS: frozenset[str] = frozenset({*LAUNCH_LANGUAGES.values(), "tsx"})


class UnknownLanguageError(KeyError):
    """Raised for a language OXN has no grammar mapping for."""


def get_language(grammar: str) -> Language:
    """Return the tree-sitter ``Language`` for a *grammar id* -- `profile.grammar`.

    A grammar id, not a language name. They coincide for five of the six launch languages,
    which is why taking the name worked for as long as it did; `tsx` is the one that differs,
    and a language name would have no way to ask for it.

    The grammar is downloaded and cached by ``tree-sitter-language-pack`` on first use,
    so the first call for a given grammar may touch the network; later calls do not.
    """
    if grammar not in GRAMMARS:
        raise UnknownLanguageError(f"{grammar!r} is not a grammar OXN parses; {sorted(GRAMMARS)}")

    from tree_sitter_language_pack import get_language as _pack_get_language

    return _pack_get_language(grammar)


def get_parser(grammar: str) -> Parser:
    """Return a tree-sitter ``Parser`` for a grammar id -- `profile.grammar`, never `.name`."""
    from tree_sitter import Parser as _Parser

    return _Parser(get_language(grammar))
