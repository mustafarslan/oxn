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
    """How to invoke one language's SCIP indexer.

    The argv is **data**, not a branch. It was a branch -- `if self.language == "python"`
    with every other indexer falling through to scip-typescript's spelling -- which is fine
    for two shapes and silently wrong for the third: `scip-go` has no `--cwd`, and
    `rust-analyzer` has no `index` subcommand at all. A fall-through default is a promise
    that every future indexer looks like the one that happened to be written second.
    """

    language: str
    command: str
    install_hint: str
    #: Arguments after the command. `{output}`, `{root}`, `{name}` and `{version}` are
    #: substituted; `run_indexer` sets the working directory to the tree being indexed, so
    #: an indexer that defaults to `.` needs no root at all.
    template: tuple[str, ...]

    def argv(self, root: Path, output: Path, project: Project) -> list[str]:
        values = {
            "output": str(output),
            "root": str(root),
            "name": project.name,
            "version": project.version,
        }
        return [self.command, *(part.format(**values) for part in self.template)]


#: Every SCIP indexer OXN can actually run, keyed by language. All Apache-2.0, free and
#: offline (ADR-0001). **This is the single registry**: `oxn doctor` reports from it rather
#: than from a list of its own. It kept a second copy until 2026-09-06, and the two had
#: drifted into advertising three indexers -- Go, Java, Rust -- that `oxn index` could not
#: invoke, under an install hint for Go that had not worked since the project changed
#: organisation. A user who followed that advice installed nothing and gained nothing.
#:
#: Every template here was read off the tool's own `--help`, never its documentation, which
#: is the same rule that produced the `--project-version` note below.
INDEXERS: dict[str, Indexer] = {
    "python": Indexer(
        "python",
        "scip-python",
        "npm install -g @sourcegraph/scip-python",
        # `--project-version` is not optional in practice: without it scip-python dies
        # inside `normalizeNameOrVersion` with an error naming none of this.
        (
            "index",
            "--project-name",
            "{name}",
            "--project-version",
            "{version}",
            "--output",
            "{output}",
            "{root}",
        ),
    ),
    "typescript": Indexer(
        "typescript",
        "scip-typescript",
        "npm install -g @sourcegraph/scip-typescript",
        ("index", "--output", "{output}", "--cwd", "{root}"),
    ),
    "javascript": Indexer(
        "javascript",
        "scip-typescript",
        "npm install -g @sourcegraph/scip-typescript",
        ("index", "--output", "{output}", "--cwd", "{root}"),
    ),
    "go": Indexer(
        "go",
        "scip-go",
        # The module moved from `sourcegraph/scip-go` to `scip-code/scip-go`, and the old
        # path does not merely 404: `go install` resolves it, downloads it, and *then*
        # refuses because the go.mod inside declares the new name. The hint OXN shipped
        # was a command that could never succeed.
        "go install github.com/scip-code/scip-go/cmd/scip-go@latest",
        # No `--cwd`: `--module-root` defaults to `.`, which is the tree we run in.
        ("index", "--output", "{output}"),
    ),
    "rust": Indexer(
        "rust",
        "rust-analyzer",
        # The binary alone is not enough, and this was learned by running it: with
        # `rust-analyzer` installed but no `cargo` on PATH, `scip` panics inside
        # `FetchMetadata::exec` and writes no index. It loads the workspace through cargo
        # rather than reading source, so the Rust *toolchain* is the real prerequisite --
        # the same shape as scip-java's build-tool dependency, and worth naming here rather
        # than leaving a user to read a Rust backtrace.
        "rustup component add rust-analyzer (needs a Rust toolchain: cargo must be on PATH)",
        # Not `index`: the SCIP emitter is a subcommand of the language server itself, and
        # it takes the tree as a positional.
        ("scip", "{root}", "--output", "{output}"),
    ),
}

#: Launch languages with no indexer OXN can run, and why. Named rather than omitted: a
#: language that is simply missing from `INDEXERS` looks like an oversight, and `oxn doctor`
#: has to be able to say "L2 is not available here, and this is what it would take".
#:
#: `scip-java` is real, free and Apache-2.0 like the rest, but it drives the project's build
#: tool -- Gradle, Maven or sbt -- rather than reading source, so it cannot be verified the
#: way the others were: by running it and reading its `--help`. It stays out until it can be.
UNWIRED: dict[str, str] = {
    "java": (
        "scip-java exists and is free, but it builds the project through Gradle/Maven/sbt "
        "and OXN has not verified its invocation against a real build. See ADR-0002."
    ),
}


def available_indexers() -> dict[str, str | None]:
    """Language -> resolved indexer path, or ``None`` when it is not installed."""
    return {language: shutil.which(indexer.command) for language, indexer in INDEXERS.items()}


@dataclass(frozen=True, slots=True)
class Project:
    """What an indexer needs to *name* the thing it is indexing.

    `--project-version` is not optional in practice: without it scip-python dies with an
    opaque error, which is the sort of thing worth encoding in a type rather than
    rediscovering. Both carry defaults because for a one-off index nobody cares what the
    project is called, and both are here because they travel together and neither has
    anything to do with where the output goes.
    """

    name: str = "project"
    version: str = "0.0.0"


def run_indexer(
    language: str,
    root: Path,
    output: Path,
    project: Project | None = None,
    *,
    timeout: int = 900,
) -> Path:
    """Produce a SCIP index for ``root``. Returns the path written."""
    project = project or Project()
    indexer = INDEXERS.get(language)
    if indexer is None:
        raise IndexerNotFound(f"no SCIP indexer configured for {language!r}")
    if shutil.which(indexer.command) is None:
        raise IndexerNotFound(
            f"{indexer.command} is not installed; get it with: {indexer.install_hint}"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        indexer.argv(root, output, project),
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if not output.exists():
        raise IndexerNotFound(f"{indexer.command} produced no index: {result.stderr.strip()[:400]}")
    _reject_empty(indexer, output, result.stderr)
    return output


def _reject_empty(indexer: Indexer, output: Path, stderr: str) -> None:
    """An index with no documents in it is a failure wearing a success's clothes.

    `scip-go` run outside a Go module warns on stderr, **exits 0**, and writes a valid SCIP
    file containing nothing. "The output file exists" was the whole success test, so that
    run produced an empty L2 rather than an error -- and every coverage number computed from
    it would have been measuring an empty set while looking entirely healthy. The failure
    mode is not hypothetical: it is how this was found, from an index of 209 bytes.

    Checked by *reading* the artifact rather than by trusting the exit code, because the
    exit code is the thing that lied.
    """
    from oxn.scip.index import load_index

    try:
        documents = len(load_index(output).documents)
    except (OSError, ValueError):  # pragma: no cover - a truncated write is caught below
        documents = 0
    if documents:
        return
    output.unlink(missing_ok=True)
    raise IndexerNotFound(
        f"{indexer.command} exited cleanly but indexed no files. For `scip-go` this is what "
        f"running outside a Go module looks like. stderr: {stderr.strip()[:300]}"
    )
