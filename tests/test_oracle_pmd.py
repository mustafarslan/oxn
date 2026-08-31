"""Duplication against PMD-CPD, the reference copy-paste detector."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from oracle_support import CORPUS
from oxn.languages import get_parser

pytestmark = pytest.mark.oracle

PMD = Path(os.environ.get("OXN_PMD_DIR", "tools/pmd-bin")) / "bin" / "pmd"

requires_pmd = pytest.mark.skipif(
    not PMD.exists(),
    reason="PMD not installed; see scripts/check.py --oracle --install",
)


def _pmd_clone_lines(directory: Path, min_tokens: int) -> set[tuple[str, int]]:
    """(path, line) for every line PMD-CPD reports as duplicated."""
    import csv
    import io

    result = subprocess.run(
        [
            str(PMD),
            "cpd",
            "--minimum-tokens",
            str(min_tokens),
            "--language",
            "python",
            "--dir",
            str(directory),
            "--format",
            "csv",
        ],
        capture_output=True,
        text=True,
    )
    lines: set[tuple[str, int]] = set()
    for row in csv.reader(io.StringIO(result.stdout)):
        if not row or row[0] == "lines":
            continue
        span = int(row[0])
        rest = row[3:]
        for index in range(0, len(rest) - 1, 2):
            start = int(rest[index])
            path = Path(rest[index + 1]).relative_to(directory).as_posix()
            lines.update((path, start + offset) for offset in range(span))
    return lines


def _oxn_clone_lines(directory: Path, min_tokens: int) -> set[tuple[str, int]]:
    from oxn.graph.sources import iter_source_files
    from oxn.profiles import profile_for_path
    from oxn.volume.clones import file_windows, find_clones

    windows = {}
    for path in iter_source_files([directory]):
        profile = profile_for_path(str(path))
        if profile is None:
            continue
        tree = get_parser(profile.name).parse(path.read_bytes())
        if tree.root_node.has_error:
            continue
        relative = path.resolve().relative_to(directory).as_posix()
        windows[relative] = file_windows(relative, tree.root_node, profile, min_tokens=min_tokens)

    report = find_clones(windows, min_tokens=min_tokens, min_lines=4)
    return {
        (clone.path, line)
        for klass in report.clone_classes
        for clone in klass.occurrences
        for line in clone.lines
    }


@requires_pmd
@pytest.mark.skipif(not CORPUS.exists(), reason="corpora not fetched")
@pytest.mark.slow
def test_duplication_recovers_what_pmd_cpd_finds() -> None:
    """Recall against PMD-CPD, not equality.

    The two tools tokenize differently and select overlapping candidates differently, so
    exact clone-class equality is not a meaningful target. What matters for a duplication
    detector is that it does not *miss* things: measured on httpx, OXN recovers 92% of the
    lines PMD reports and flags more besides. See docs/divergences.md.
    """
    directory = (CORPUS / "httpx").resolve()
    theirs = _pmd_clone_lines(directory, 50)
    ours = _oxn_clone_lines(directory, 50)

    assert theirs, "PMD reported nothing; the oracle is misconfigured"
    recall = len(ours & theirs) / len(theirs)
    jaccard = len(ours & theirs) / len(ours | theirs)

    assert recall >= 0.85, f"recall {recall:.2%} of PMD's duplicated lines"
    assert jaccard >= 0.50, f"line-level Jaccard {jaccard:.2f}"


@requires_pmd
@pytest.mark.skipif(not CORPUS.exists(), reason="corpora not fetched")
@pytest.mark.slow
def test_duplication_agrees_with_pmd_on_which_files_are_affected() -> None:
    directory = (CORPUS / "httpx").resolve()
    theirs = {path for path, _ in _pmd_clone_lines(directory, 50)}
    ours = {path for path, _ in _oxn_clone_lines(directory, 50)}
    jaccard = len(ours & theirs) / len(ours | theirs)
    assert jaccard >= 0.60, f"file-level Jaccard {jaccard:.2f}: {sorted(theirs ^ ours)}"
