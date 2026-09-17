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

import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path


class IndexerError(RuntimeError):
    """Anything that stopped a SCIP index being produced."""


class IndexerNotFound(IndexerError):
    """Raised when no SCIP indexer is available for a language."""


class IndexerTimeout(IndexerError):
    """Raised when an indexer outran its time budget and was killed."""


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
    #: Glob, matched at the tree's root only, for extra positional *projects* to index.
    #:
    #: **An indexer is given projects, not files, and one project is rarely the whole tree.**
    #: `scip-typescript` indexes the `tsconfig.json` it finds and nothing else, so on
    #: `typescript-nest` it covered 1,020 files of 1,913: every `*.spec.ts` was missing,
    #: because the tests are declared in a sibling `tsconfig.spec.json` that nothing asked it
    #: to read. Appending the root's other configs recovers 424 of them.
    #:
    #: Root-level and non-recursive on purpose. Walking the tree would pick up the tsconfig
    #: of every example application under `sample/` and index each as a project in its own
    #: right, which is not what the repository declares itself to be. An unreadable project
    #: is safe to pass: `scip-typescript` reports `- <path> (missing tsconfig.json)`, skips
    #: it, and still writes the index -- verified rather than assumed, since the alternative
    #: is that one stale config costs a project its entire index.
    projects: str = ""
    #: Files this indexer writes into the tree that OXN must remove again.
    #:
    #: **A tool that measures a repository may not modify it.** `scip-typescript
    #: --infer-tsconfig` writes a `tsconfig.json` when it finds none, and OXN passes that flag
    #: for JavaScript because without it the indexer reports "no files got indexed" and writes
    #: nothing at all. So `oxn index` was leaving a config file behind in every JavaScript
    #: project it touched -- `javascript-eslint` carried one, untracked, dated to the day the
    #: entry was fixed, and *every JavaScript figure this project published was measured
    #: against it* rather than against the repository's own `tsconfig.base.json`.
    #:
    #: Only a file OXN's own run created is removed. One the project already had is its own.
    leaves_behind: tuple[str, ...] = ()
    #: Seconds this indexer gets before it is killed.
    #:
    #: Per language because the spread is two orders of magnitude and it is a property of the
    #: tool, not of the call site -- every caller used to pass its own number (600, 1,200,
    #: 1,800, 2,400), which is the same knowledge written down four times and agreeing by
    #: luck. Go indexes `go-kit` in 1 s and `scip-java` takes 361 s on 4,214 lines, because
    #: `scip-java` runs Maven and is paying for dependency resolution and compilation rather
    #: than for indexing.
    timeout: int = 900
    #: What running this indexer costs the *user's* machine, in one sentence, or empty when
    #: it costs nothing worth saying.
    #:
    #: Only `scip-java` has one, and it is the difference between a tool that indexes and a
    #: tool that *builds*: `oxn index --language java` runs Maven or Gradle over the user's
    #: repository, and until this field existed nothing told them. The measured cost is 361 s
    #: on 4,214 lines, `_spawn` captures the build's output rather than streaming it, and the
    #: budget above is 2,400 s -- so the observable behaviour was a command that printed
    #: nothing for up to forty minutes on a first run. Go's 1 s and rust-analyzer's 40 s are
    #: not caveats and this stays empty for them, because a warning on every language is a
    #: warning on none.
    caveat: str = ""

    def argv(self, root: Path, output: Path, project: Project) -> list[str]:
        values = {
            "output": str(output),
            "root": str(root),
            "name": project.name,
            "version": project.version,
        }
        return [
            self.command,
            *(part.format(**values) for part in self.template),
            *self.root_projects(root),
        ]

    def root_projects(self, root: Path) -> list[str]:
        """The project files to append, sorted so the same tree gives the same argv.

        Sorted also fixes *which* copy of a doubly-indexed file survives ingest: the parser
        keeps the first document for a path, and `tsconfig.json` sorts before its siblings.
        """
        if not self.projects:
            return []
        return sorted(found.name for found in root.glob(self.projects) if found.is_file())


#: Every SCIP indexer OXN can actually run, keyed by language. All Apache-2.0, free and
#: offline (ADR-0001). **This is the single registry**: `oxn doctor` reports from it rather
#: than from a list of its own. It kept a second copy until 2026-09-06, and the two had
#: drifted into advertising three indexers -- Go, Java, Rust -- that `oxn index` could not
#: invoke, under an install hint for Go that had not worked since the project changed
#: organisation. A user who followed that advice installed nothing and gained nothing.
#:
#: **The rule for being in here is that the argv was read off the tool's own `--help`**, never
#: its documentation -- the rule that produced the `--project-version` note below.
#:
#: Both of the caveats this note used to carry are gone, and leaving them would be the same
#: drift the paragraph above is about. `rust-analyzer` has since indexed `ripgrep` in 40 s,
#: and `scip-java` is in: its argv *is* a single verified `index --output <path>`, and the
#: build-tool ambiguity is a refusal `scip-java` makes at run time on repositories that
#: declare two builds, not a reason it could not be wired.
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
        projects="tsconfig*.json",
    ),
    "javascript": Indexer(
        "javascript",
        "scip-typescript",
        "npm install -g @sourcegraph/scip-typescript",
        # `--infer-tsconfig`, and without it this entry could never have indexed anything.
        # It was a copy of the TypeScript row, which is right about the binary and wrong
        # about the flag: a JavaScript project has no `tsconfig.json`, and scip-typescript
        # then exits with "no files got indexed" rather than writing an empty index. So
        # JavaScript was wired, listed by `oxn doctor`, and unusable. Measured on a
        # two-file CommonJS package: 0 bytes without the flag, a 2,158-byte index with it.
        #
        # The flag stays, and is inert whenever `projects` finds anything: scip-typescript
        # infers a config only when it was given none. It is still what makes a tree with no
        # TypeScript configuration at all indexable.
        ("index", "--infer-tsconfig", "--output", "{output}", "--cwd", "{root}"),
        projects="tsconfig*.json",
        leaves_behind=("tsconfig.json",),
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
        # rather than reading source, so the Rust *toolchain* is the real prerequisite.
        # With cargo present it indexes `ripgrep` in 40 s.
        "rustup component add rust-analyzer (needs a Rust toolchain: cargo must be on PATH)",
        # Not `index`: the SCIP emitter is a subcommand of the language server itself, and
        # it takes the tree as a positional.
        ("scip", "{root}", "--output", "{output}"),
    ),
    "java": Indexer(
        "java",
        "scip-java",
        # Three things wrong with the obvious hint, all found by following it. The plain
        # `coursier install scip-java` fails: the app is not in the default channel, so it
        # needs `--contrib`. Coursier then installs to `~/Library/Application Support/
        # Coursier/bin` (`~/.local/share/coursier/bin` on Linux), which it does not add to
        # PATH -- so `shutil.which` reports the tool missing on a machine that has just
        # installed it, and the hint has to say so or the user runs the same command twice.
        "coursier install --contrib scip-java, then add coursier's bin directory to PATH "
        "(~/Library/Application Support/Coursier/bin, or ~/.local/share/coursier/bin); "
        "needs a JDK and the project's build tool",
        # It runs the project's build, and that is the whole cost: 361 s on petclinic's
        # 4,214 lines, which is Maven resolving and compiling rather than anything slow about
        # indexing. Java L2 is a batch operation.
        #
        # No `--build-tool`, deliberately. A repository holding both a `pom.xml` and a
        # `build.gradle` -- `java-spring-petclinic` does -- makes `scip-java` refuse with
        # "Multiple build tools detected" rather than pick one. Detecting the single-build
        # case would add nothing, because `scip-java` already detects it; the only case the
        # flag decides is the ambiguous one, and guessing there is how you run the wrong
        # build for ten minutes. The error is scip-java's own, it names the flag, and
        # `run_indexer` puts it in front of the user, who has `oxn index --index-file` for
        # an index they built themselves.
        ("index", "--output", "{output}"),
        # ~6.6x the 361 s measured cold on petclinic's 4,214 lines, and the margin is not a
        # multiple of the code size: most of that wall clock is Maven resolving dependencies,
        # which does not scale with LOC, while compilation does. A real repository is larger
        # in the half that scales, so the budget is generous on purpose -- being killed
        # halfway through a build wastes everything already spent on it.
        timeout=2400,
        caveat="runs this project's build (Maven or Gradle), not just an index",
    ),
}

#: Launch languages with no indexer OXN can run, and why. Named rather than omitted: a
#: language that is simply missing from `INDEXERS` looks like an oversight, and `oxn doctor`
#: has to be able to say "L2 is not available here, and this is what it would take".
#:
#: Empty as of 2026-09-06: every launch language has an indexer OXN can invoke. Kept because
#: the *distinction* is what `doctor` needs -- "not installed" and "not supported" are
#: different answers, and only one of them is fixed by running a command.
UNWIRED: dict[str, str] = {}


def resolve_indexer(language: str) -> Indexer:
    """The indexer for ``language``, or raise -- without running anything.

    Split out of `run_indexer` so a caller can learn *what it is about to start* before it
    starts. `oxn index --language java` runs the user's build, and the warning saying so may
    only be printed when it is true: inside `run_indexer` the lookup and the `which` check
    happened after the point where anyone could have spoken, so a caller printing beforehand
    would have warned about a Maven build on machines with no `scip-java` installed at all.

    Both failures stay here rather than at the call site, so there is one wording for "no
    indexer for this language" and one for "not installed", and `run_indexer` keeps raising
    exactly what it raised before.
    """
    indexer = INDEXERS.get(language)
    if indexer is None:
        raise IndexerNotFound(f"no SCIP indexer configured for {language!r}")
    if shutil.which(indexer.command) is None:
        raise IndexerNotFound(
            f"{indexer.command} is not installed; get it with: {indexer.install_hint}"
        )
    return indexer


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
    timeout: int | None = None,
) -> Path:
    """Produce a SCIP index for ``root``. Returns the path written.

    ``timeout`` defaults to the indexer's own budget (`Indexer.timeout`), which is where the
    knowledge of how long a language takes belongs.
    """
    project = project or Project()
    indexer = resolve_indexer(language)

    output.parent.mkdir(parents=True, exist_ok=True)
    existing = {name for name in indexer.leaves_behind if (root / name).exists()}
    try:
        stderr = _spawn(indexer, indexer.argv(root, output, project), root, timeout)
    finally:
        # In `finally` because a run that failed is exactly when nobody looks, and the file
        # would survive precisely then.
        _remove_artifacts(root, indexer.leaves_behind, existing)
    if not output.exists():
        raise IndexerNotFound(f"{indexer.command} produced no index: {stderr.strip()[:400]}")
    _reject_empty(indexer, output, stderr)
    return output


def _spawn(indexer: Indexer, argv: list[str], root: Path, timeout: int | None) -> str:
    """Run the indexer to completion, or kill its whole process tree and say so.

    **`subprocess.run(timeout=...)` kills the child it started and nothing beneath it.** An
    indexer is rarely one process: `scip-java` is a JVM that runs Maven that runs `javac`, so
    a timeout left the build running with nobody waiting for it -- measured, two survivors
    from a two-process fake. Its own session makes the tree one process group, and the group
    is what gets signalled.

    **And the orphans were the milder half.** Reverting this to `process.kill()` does not
    merely leak processes: the survivors still hold the inherited `stdout` and `stderr`, so
    the `communicate()` that follows blocks until they exit on their own. A Java index that
    outran its budget would hang for as long as the build felt like running, having already
    been given up on. Killing the group closes the pipes.
    """
    budget = timeout or indexer.timeout
    process = subprocess.Popen(  # noqa: S603 - argv is this module's own data
        argv,
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _, stderr = process.communicate(timeout=budget)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        process.communicate()
        raise IndexerTimeout(
            f"{indexer.command} exceeded its {budget}s budget and was stopped. Indexing "
            f"{indexer.language} can mean running the project's build -- `scip-java` runs "
            "Maven -- so this is a build's cost rather than an indexer's. Build a SCIP index "
            "separately and pass it with `oxn index --index-file <path>`."
        ) from None
    return stderr


def _remove_artifacts(root: Path, named: tuple[str, ...], existing: set[str]) -> None:
    """Delete the files this run created in the tree, and only those.

    A config the project already had is the project's, even if the indexer would have written
    an identical one; `existing` is sampled before the run for exactly that reason.
    """
    for name in named:
        if name not in existing:
            (root / name).unlink(missing_ok=True)


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
