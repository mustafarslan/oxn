"""Loading the project a bundle is built from -- once, for every surface that needs it.

`oxn context` and the MCP server ask the same question, and asking it twice in two places
is how the CLI and the server start disagreeing about what a project is. The walk, the
decisions and the exclusions live here so that both surfaces get the same answer or neither
does.

Nothing here is cached. ADR-0004 reserves warm state for the MCP server and leaves *when*
to refresh open until the report path has been measured on a repository larger than this
one; caching before that measurement would be guessing at the refresh strategy the ADR
explicitly defers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from oxn.context.bundle import Project

if TYPE_CHECKING:  # pragma: no cover
    from oxn.config import Config


def load_project(config: Config) -> Project:
    """Everything `build_bundle` needs: the config, the decisions, and the tree it governs."""
    from oxn.graph.sources import iter_source_files
    from oxn.rules.adr import load_decisions

    paths = iter_source_files([config.root], base=config.root, exclude=config.exclude)
    return Project(
        config=config,
        decisions=tuple(load_decisions(config.root)),
        paths=tuple(str(path.relative_to(config.root)) for path in paths),
    )
