"""Detect and run SCIP indexers.

ADR-0002 is explicit: OXN **detects and instructs, never installs**. If an indexer is
missing, `oxn doctor` prints the command to get it and analysis carries on at L0/L1 with
the affected metrics marked approximate.

Each indexer has its own operational quirks, discovered by running them rather than by
reading their docs -- `scip-python` fails with an opaque
``Cannot read properties of undefined`` unless a project *version* is supplied, which no
documentation mentions.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class IndexerNotFound(RuntimeError):
    """Raised when no SCIP indexer is available for a language."""


@dataclass(frozen=True, slots=True)
class Indexer:
    """How to invoke one language's SCIP indexer."""

    language: str
    command: str
    install_hint: str

    def argv(self, root: Path, output: Path, project_name: str, project_version: str) -> list[str]:
        if self.language == "python":
            # `--project-version` is not optional in practice: without it scip-python dies
            # inside `normalizeNameOrVersion` with an error naming none of this.
            return [
                self.command,
                "index",
                "--project-name",
                project_name,
                "--project-version",
                project_version,
                "--output",
                str(output),
                str(root),
            ]
        return [self.command, "index", "--output", str(output), "--cwd", str(root)]


INDEXERS: dict[str, Indexer] = {
    "python": Indexer("python", "scip-python", "npm install -g @sourcegraph/scip-python"),
    "typescript": Indexer(
        "typescript", "scip-typescript", "npm install -g @sourcegraph/scip-typescript"
    ),
    "javascript": Indexer(
        "javascript", "scip-typescript", "npm install -g @sourcegraph/scip-typescript"
    ),
}


def available_indexers() -> dict[str, str | None]:
    """Language -> resolved indexer path, or ``None`` when it is not installed."""
    return {language: shutil.which(indexer.command) for language, indexer in INDEXERS.items()}


def run_indexer(
    language: str,
    root: Path,
    output: Path,
    *,
    project_name: str = "project",
    project_version: str = "0.0.0",
    timeout: int = 900,
) -> Path:
    """Produce a SCIP index for ``root``. Returns the path written."""
    indexer = INDEXERS.get(language)
    if indexer is None:
        raise IndexerNotFound(f"no SCIP indexer configured for {language!r}")
    if shutil.which(indexer.command) is None:
        raise IndexerNotFound(
            f"{indexer.command} is not installed; get it with: {indexer.install_hint}"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        indexer.argv(root, output, project_name, project_version),
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if not output.exists():
        raise IndexerNotFound(f"{indexer.command} produced no index: {result.stderr.strip()[:400]}")
    return output
