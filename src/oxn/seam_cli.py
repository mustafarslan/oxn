"""The `oxn seam` command's runner: build the file graph, then call `oxn.graph.seam` (DX-X1).

A surface, like `report.py`: it reads the repository and prints; the decision is the graph
layer's. It writes nothing.
"""

from __future__ import annotations

import json
import os
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from oxn.graph.seam import SeamInput, seam
from oxn.graph.sources import DEFAULT_EXCLUDES
from oxn.profiles import profile_for_path

#: Source suffixes OXN has no profile for. A file with one of these in scope means the graph is
#: missing part of the code, so the answer must be solo. Text, data and config files are not code.
UNPARSED_CODE: dict[str, str] = {
    ".swift": "swift", ".kt": "kotlin", ".kts": "kotlin", ".m": "objective-c", ".mm": "objective-c",
    ".c": "c", ".h": "c", ".cc": "c++", ".cpp": "c++", ".hpp": "c++", ".cs": "c#", ".rb": "ruby",
    ".php": "php", ".scala": "scala", ".dart": "dart", ".ex": "elixir", ".exs": "elixir", ".lua": "lua",
}


def _in_scope(path: str, scope: list[str]) -> bool:
    return not scope or any(fnmatch(path, pattern) for pattern in scope)


def _unparsed(root: Path, scope: list[str]) -> set[str]:
    """Languages of the code files in scope that OXN can't parse."""
    found: set[str] = set()
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in DEFAULT_EXCLUDES)
        for name in files:
            relative = os.path.relpath(os.path.join(folder, name), root).replace(os.sep, "/")
            language = UNPARSED_CODE.get(Path(name).suffix)
            if language and _in_scope(relative, scope):
                found.add(language)
    return found


def _file_graph() -> tuple[Path, dict[str, frozenset[str]], list[str]]:
    """The repository's root, its file-level import graph, and the source of each unresolved import."""
    from oxn.graph.depgraph import build_dependency_graph
    from oxn.report import _indexer

    with _indexer() as indexer:
        root = Path(indexer.root)
        indexer.index([root])
        graph = build_dependency_graph(indexer.root, indexer.sources([root]), component_of=lambda path: path)
    edges = {source: frozenset(targets) for source, targets in graph.files.items()}
    return root, edges, [item.source for item in graph.unresolved]


def _unresolved_share(in_scope: frozenset[str], edges: dict[str, frozenset[str]], unresolved: list[str]) -> float:
    """Unresolved imports out of all imports made by the scope's files."""
    missing = sum(1 for source in unresolved if source in in_scope)
    total = missing + sum(len(edges.get(path, ())) for path in in_scope)
    return missing / total if total else 0.0


def run_seam(scope: list[str], commons: list[str], lanes_max: int, min_lane: int) -> dict[str, Any]:
    """Seam the repository at the configured root; the schema v1 answer."""
    root, edges, unresolved = _file_graph()
    files = set(edges).union(*edges.values()) if edges else set()
    in_scope = frozenset(path for path in files if _in_scope(path, scope))
    seen = {profile.name for profile in map(profile_for_path, in_scope) if profile is not None}
    return seam(
        SeamInput(
            edges=edges,
            scope=in_scope,
            commons=tuple(commons),
            lanes_max=lanes_max,
            min_lane=min_lane,
            unresolved_share=_unresolved_share(in_scope, edges, unresolved),
            languages_seen=tuple(sorted(seen)),
            languages_unparsed=tuple(sorted(_unparsed(root, scope))),
        )
    )


def seam_json(payload: dict[str, Any]) -> str:
    """One line, keys sorted: the same input gives the same bytes."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
