"""Which SCIP indexers OXN can run, and the two ways that answer used to be wrong.

`oxn doctor` reports the environment and `oxn index` acts on it, and until 2026-09-06 they
read from **two different lists**. The report advertised `scip-go`, `scip-java` and
`rust-analyzer` under "install this to enable exact coupling metrics"; `oxn index` had
entries for none of them, so a user who followed the advice installed a tool OXN would then
refuse to invoke. The Go hint was also stale in a way that cannot be seen by reading it: the
project changed organisation, and `go install github.com/sourcegraph/scip-go/...` resolves,
downloads, and *then* fails on the module path declared inside.

So the tests here are about agreement rather than behaviour. A second list is a list that
drifts, and the only durable fix is that there is one — asserted, because "we merged them"
is not a property a future edit preserves.

The argv shapes are the other half. They were a branch on `self.language == "python"` with
everything else falling through to scip-typescript's spelling, which is correct for the two
indexers that existed and wrong for both that were added: `scip-go` has no `--cwd` and
`rust-analyzer` has no `index` subcommand at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oxn.languages import LAUNCH_LANGUAGES
from oxn.scip.runner import INDEXERS, UNWIRED, IndexerNotFound, Project, run_indexer


def test_doctor_and_the_runner_cannot_disagree() -> None:
    """The defect this file exists for: two lists, one of them advertising indexers the
    other could not invoke."""
    from oxn.doctor import check_indexers

    assert set(check_indexers()) == set(INDEXERS)


def test_every_launch_language_is_accounted_for() -> None:
    """A language missing from both tables reads as an oversight, and `oxn doctor` would say
    nothing about it at all -- neither "install this" nor "not available"."""
    assert set(LAUNCH_LANGUAGES) == set(INDEXERS) | set(UNWIRED)


def test_java_is_declared_unavailable_rather_than_quietly_absent() -> None:
    """ "Not installed" and "OXN cannot use this" are different answers to a user, because no
    command fixes the second. `scip-java` drives Gradle/Maven/sbt rather than reading source,
    so its invocation has not been verified the way the others were."""
    assert "java" in UNWIRED
    assert "java" not in INDEXERS
    assert "build" in UNWIRED["java"].lower()


def test_the_go_install_hint_names_the_module_path_that_resolves() -> None:
    """`go install github.com/sourcegraph/scip-go/...` cannot succeed and never could: the
    module declares itself as `github.com/scip-code/scip-go`, so the download completes and
    the build refuses. OXN printed that command for a phase and a half."""
    hint = INDEXERS["go"].install_hint

    assert "scip-code/scip-go" in hint
    assert "sourcegraph/scip-go" not in hint


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            "python",
            # `--project-version` is not optional in practice; without it scip-python dies
            # inside `normalizeNameOrVersion`.
            "scip-python index --project-name p --project-version 1 --output /o.scip /tree",
        ),
        ("typescript", "scip-typescript index --output /o.scip --cwd /tree"),
        # No `--cwd` flag exists; `--module-root` defaults to the working directory.
        ("go", "scip-go index --output /o.scip"),
        # Not an `index` subcommand: SCIP emission is a mode of the language server, and the
        # tree is a positional.
        ("rust", "rust-analyzer scip /tree --output /o.scip"),
    ],
)
def test_each_indexer_gets_the_argv_its_own_help_documents(language: str, expected: str) -> None:
    """Every one of these was read off the tool's `--help`, never its documentation — the
    same rule that turned up scip-python's `--project-version`."""
    argv = INDEXERS[language].argv(Path("/tree"), Path("/o.scip"), Project("p", "1"))

    assert " ".join(argv) == expected


def test_an_unknown_language_is_refused_by_name(tmp_path: Path) -> None:
    with pytest.raises(IndexerNotFound, match="cobol"):
        run_indexer("cobol", tmp_path, tmp_path / "out.scip")


def test_an_index_with_no_documents_is_a_failure(tmp_path: Path, monkeypatch) -> None:
    """The quirk that makes this necessary: `scip-go` run outside a Go module warns on
    stderr, **exits 0**, and writes a valid SCIP file containing nothing.

    "The output file exists" was the entire success test, so that run produced an empty L2
    instead of an error -- and every coverage number computed from it would have measured an
    empty set while looking healthy. Found from a 209-byte index, not predicted.
    """
    import subprocess

    from oxn.scip import runner

    output = tmp_path / "out.scip"

    def fake_run(argv, **kwargs):
        output.write_bytes(b"")  # a well-formed index of nothing
        return subprocess.CompletedProcess(argv, 0, "", "no go.mod file found")

    monkeypatch.setattr(runner.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    with pytest.raises(IndexerNotFound, match="indexed no files"):
        run_indexer("go", tmp_path, output)

    assert not output.exists(), "an index rejected as empty must not be left on disk"
