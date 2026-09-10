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


class UnknownLanguageError(KeyError):
    """Raised for a language OXN has no grammar mapping for."""


def get_language(name: str) -> Language:
    """Return the tree-sitter ``Language`` for an OXN language name.

    The grammar is downloaded and cached by ``tree-sitter-language-pack`` on first use,
    so the first call for a given language may touch the network; later calls do not.
    """
    try:
        grammar_id = LAUNCH_LANGUAGES[name]
    except KeyError as exc:
        raise UnknownLanguageError(
            f"{name!r} is not a launch language; known: {sorted(LAUNCH_LANGUAGES)}"
        ) from exc

    from tree_sitter_language_pack import get_language as _pack_get_language

    return _pack_get_language(grammar_id)


def get_parser(name: str) -> Parser:
    """Return a tree-sitter ``Parser`` configured for an OXN language name."""
    from tree_sitter import Parser as _Parser

    return _Parser(get_language(name))
