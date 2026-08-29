"""Shared fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

    from oxn.graph.model import ParsedFile


@pytest.fixture
def build() -> Callable[[str, str], ParsedFile]:
    """Parse a snippet into a graph fragment: ``build(language, source)``."""
    from oxn.graph.builder import build_file
    from oxn.languages import get_parser
    from oxn.profiles import get_profile

    def _build(language: str, source: str, path: str | None = None) -> ParsedFile:
        profile = get_profile(language)
        suffix = sorted(profile.extensions)[0]
        data = source.encode()
        tree = get_parser(language).parse(data)
        return build_file(path or f"pkg/sample{suffix}", data, profile, tree.root_node)

    return _build


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A tiny polyglot working tree, including a directory that must be excluded."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f(x):\n    return x\n")
    (tmp_path / "src" / "b.py").write_text("class C:\n    def m(self):\n        pass\n")
    (tmp_path / "src" / "c.ts").write_text("export function g(a: number) { return a; }\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.ts").write_text("export const x = 1;\n")
    (tmp_path / "README.md").write_text("# not source\n")
    return tmp_path
