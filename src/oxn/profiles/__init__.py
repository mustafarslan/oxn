"""Language profile registry.

Profiles are looked up by OXN language name or by file extension. A language without a
profile is *not supported*, even if a grammar exists for it -- the grammar alone cannot
tell the graph builder what a function is.
"""

from __future__ import annotations

from oxn.profiles.base import LanguageProfile, Wrapper
from oxn.profiles.go import GO
from oxn.profiles.java import JAVA
from oxn.profiles.python import PYTHON
from oxn.profiles.rust import RUST
from oxn.profiles.typescript import JAVASCRIPT, TSX, TYPESCRIPT

#: Every language OXN can analyse. A profile, not a grammar, is what makes a language
#: supported: the grammar alone cannot tell the graph builder what a function is.
PROFILES: dict[str, LanguageProfile] = {
    profile.name: profile for profile in (PYTHON, TYPESCRIPT, JAVASCRIPT, GO, RUST, JAVA)
}

#: `TSX` is here and deliberately *not* in `PROFILES`: that registry is keyed by
#: `profile.name`, which TSX shares with TYPESCRIPT on purpose, so adding it would silently
#: overwrite the entry every by-name lookup returns. Extension is the only axis on which the
#: two differ, and this is the map that is keyed by it.
_BY_EXTENSION: dict[str, LanguageProfile] = {
    ext: profile for profile in (*PROFILES.values(), TSX) for ext in profile.extensions
}


class UnsupportedLanguageError(KeyError):
    """Raised for a language OXN has a grammar for but no profile."""


def get_profile(name: str) -> LanguageProfile:
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise UnsupportedLanguageError(
            f"no LanguageProfile for {name!r}; supported: {sorted(PROFILES)}"
        ) from exc


def profile_for_path(path: str) -> LanguageProfile | None:
    """Profile for a file path, or ``None`` if OXN does not analyse this file type."""
    from pathlib import PurePath

    return _BY_EXTENSION.get(PurePath(path).suffix)


__all__ = [
    "PROFILES",
    "LanguageProfile",
    "UnsupportedLanguageError",
    "Wrapper",
    "get_profile",
    "profile_for_path",
]
