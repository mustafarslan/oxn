"""The git-log parser, against a repository built by the test.

All the correctness risk of the VCS tier is in parsing, so this builds a real repository
with exactly the cases that break naive parsers -- a rename, a binary file, an oversized
commit, a merge, two authors -- and asserts exact numbers. A synthetic fixture beats an
oracle here: code-maat can only tell us we agree with code-maat.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from oxn.vcs.analysis import (
    bus_factor,
    change_coupling,
    file_histories,
    hotspots,
    ownership,
    sum_of_coupling,
)
from oxn.vcs.log import parse_log, read_log, source_changes


def git(repo: Path, *args: str, **env: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(repo),
            "GIT_AUTHOR_NAME": env.get("name", "Ada"),
            "GIT_AUTHOR_EMAIL": env.get("email", "ada@example.com"),
            "GIT_COMMITTER_NAME": env.get("name", "Ada"),
            "GIT_COMMITTER_EMAIL": env.get("email", "ada@example.com"),
            "GIT_AUTHOR_DATE": env.get("date", "2026-01-01T00:00:00+00:00"),
            "GIT_COMMITTER_DATE": env.get("date", "2026-01-01T00:00:00+00:00"),
        },
    )


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    """A repository containing every case that breaks a naive log parser."""
    root = tmp_path_factory.mktemp("history")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Ada")
    git(root, "config", "user.email", "ada@example.com")

    # 1. two files that always change together
    # helper.py is deliberately substantial: git detects a rename by *similarity*, so a
    # two-line file whose content half-changes is reported as a delete plus an add, and
    # the parser would never see a rename to handle.
    helper_body = "\n".join(f"def helper_{index}():\n    return {index}\n" for index in range(20))
    (root / "core.py").write_text("def a():\n    return 1\n")
    (root / "helper.py").write_text(helper_body)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "first", date="2026-01-01T00:00:00+00:00")

    for index in range(2, 7):
        (root / "core.py").write_text(f"def a():\n    return {index}\n")
        (root / "helper.py").write_text(helper_body + f"\n# revision {index}\n")
        git(root, "add", "-A")
        git(root, "commit", "-qm", f"paired {index}", date=f"2026-01-0{index}T00:00:00+00:00")

    # 2. a second author, so ownership has something to measure
    (root / "core.py").write_text("def a():\n    return 99\n")
    git(root, "add", "-A")
    git(
        root,
        "commit",
        "-qm",
        "by grace",
        name="Grace",
        email="grace@example.com",
        date="2026-01-07T00:00:00+00:00",
    )

    # 3. a rename plus a small edit, which must not split the file's history
    git(root, "mv", "helper.py", "renamed.py")
    (root / "renamed.py").write_text(helper_body + "\n# renamed and edited\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "rename helper", date="2026-01-08T00:00:00+00:00")

    # 4. a binary file, which reports `-` instead of line counts
    (root / "blob.py").write_bytes(b"\x00\x01\x02\x03binary\x00")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "binary", date="2026-01-09T00:00:00+00:00")

    # 5. an oversized commit that coupling analysis must ignore
    for index in range(40):
        (root / f"bulk{index}.py").write_text(f"x = {index}\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "bulk reformat", date="2026-01-10T00:00:00+00:00")

    # 6. a merge commit, which --no-merges must skip
    git(root, "checkout", "-q", "-b", "side")
    (root / "side.py").write_text("y = 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "side work", date="2026-01-11T00:00:00+00:00")
    git(root, "checkout", "-q", "main")
    git(
        root, "merge", "-q", "--no-ff", "side", "-m", "merge side", date="2026-01-12T00:00:00+00:00"
    )

    # 7. a commit message containing the characters a naive delimiter would break on
    (root / "core.py").write_text("def a():\n    return 100\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "fix: a => b | tricky\tmessage", date="2026-01-13T00:00:00+00:00")
    return root


@pytest.fixture(scope="module")
def history(repo: Path):
    return read_log(repo)


def test_merges_are_excluded(history) -> None:
    subjects = [commit.subject for commit in history.commits]
    assert "merge side" not in subjects
    assert "side work" in subjects


def test_commit_messages_with_delimiters_survive(history) -> None:
    """A printable separator would have split this message; the parser uses NUL."""
    subjects = [commit.subject for commit in history.commits]
    assert "fix: a => b | tricky\tmessage" in subjects


def test_rename_keeps_one_continuous_history(history, repo) -> None:
    """helper.py became renamed.py; its churn must not be split across two names.

    Guarded by a precondition, because git detects renames by similarity: if the fixture
    ever stops producing a detectable rename, this must fail loudly rather than pass by
    accident.
    """
    renames = [
        change
        for commit in history.commits
        for change in commit.changes
        if change.old_path == "helper.py"
    ]
    assert renames, "fixture no longer produces a git-detected rename"

    histories = file_histories(history.commits)
    assert "helper.py" not in histories, "history split at the rename"
    assert histories["renamed.py"].commits >= 6


def test_binary_files_do_not_break_parsing(history) -> None:
    binary = [
        change
        for commit in history.commits
        for change in commit.changes
        if change.path == "blob.py"
    ]
    assert binary and binary[0].added == 0 and binary[0].deleted == 0


def test_churn_counts_added_and_deleted(history) -> None:
    histories = file_histories(history.commits)
    core = histories["core.py"]
    assert core.commits == 8
    assert core.added > 0
    assert core.churn == core.added + core.deleted


def test_authors_are_attributed(history) -> None:
    histories = file_histories(history.commits)
    assert set(histories["core.py"].authors) == {"Ada", "Grace"}


def test_change_coupling_finds_the_paired_files(history) -> None:
    couplings = change_coupling(history.commits, min_support=3, min_confidence=0.3)
    pairs = {(c.first, c.second) for c in couplings}
    assert ("core.py", "helper.py") in pairs or ("core.py", "renamed.py") in pairs


def test_oversized_commits_are_excluded_from_coupling(history) -> None:
    """One 40-file reformat would otherwise couple every bulk file to every other."""
    couplings = change_coupling(history.commits, min_support=1, min_confidence=0.0)
    bulk = [c for c in couplings if c.first.startswith("bulk") and c.second.startswith("bulk")]
    assert not bulk, f"{len(bulk)} spurious couplings from the bulk commit"


def test_including_oversized_commits_would_have_coupled_them(history) -> None:
    """Confirms the previous test is actually exercising the filter."""
    couplings = change_coupling(
        history.commits, min_support=1, min_confidence=0.0, max_commit_size=1000
    )
    bulk = [c for c in couplings if c.first.startswith("bulk") and c.second.startswith("bulk")]
    assert bulk, "the bulk commit should couple its files when the filter is lifted"


def test_sum_of_coupling_identifies_change_magnets(history) -> None:
    couplings = change_coupling(history.commits, min_support=3, min_confidence=0.3)
    totals = sum_of_coupling(couplings)
    assert totals
    assert max(totals, key=lambda path: totals[path]) in {"core.py", "renamed.py", "helper.py"}


def test_ownership_and_bus_factor(history) -> None:
    owners = ownership(file_histories(history.commits))
    assert owners["core.py"].top_author == "Ada"
    assert owners["core.py"].author_count == 2
    assert 0.5 < owners["core.py"].top_share < 1.0
    assert bus_factor(owners) >= 1


def test_hotspots_rank_by_change_frequency_times_complexity(history) -> None:
    histories = file_histories(history.commits)
    complexity = {"core.py": 20.0, "renamed.py": 1.0, "bulk0.py": 30.0}
    ranked = hotspots(histories, complexity)
    assert ranked[0].path == "core.py", "the frequently changed complex file must rank first"
    assert ranked[0].commits == 8


def test_source_changes_filters_unanalysed_paths(history) -> None:
    filtered = source_changes(history.commits, exclude=("bulk",))
    paths = {change.path for commit in filtered for change in commit.changes}
    assert not any(path.startswith("bulk") for path in paths)
    assert "core.py" in paths


# ---- parser units, for cases the fixture cannot produce on demand ----------------------


def test_brace_rename_syntax_is_normalized() -> None:
    log = (
        "\x1esha\x00Ada\x00a@b\x002026-01-01T00:00:00+00:00\x00subject\n"
        "1\t2\tsrc/{old => new}/file.py\n"
    )
    history = parse_log(log)
    change = history.commits[0].changes[0]
    assert change.old_path == "src/old/file.py"
    assert change.path == "src/new/file.py"


def test_plain_rename_syntax_is_normalized() -> None:
    log = "\x1esha\x00Ada\x00a@b\x002026-01-01T00:00:00+00:00\x00subject\n1\t2\told.py => new.py\n"
    change = parse_log(log).commits[0].changes[0]
    assert (change.old_path, change.path) == ("old.py", "new.py")


def test_rename_chains_resolve_to_the_current_name() -> None:
    """a -> b in one commit, b -> c in another: every record must land on c."""
    log = (
        "\x1es2\x00Ada\x00a@b\x002026-01-02T00:00:00+00:00\x00second\n1\t1\tb.py => c.py\n"
        "\x1es1\x00Ada\x00a@b\x002026-01-01T00:00:00+00:00\x00first\n1\t1\ta.py => b.py\n"
    )
    histories = file_histories(parse_log(log).commits)
    assert set(histories) == {"c.py"}
    assert histories["c.py"].commits == 2


# ---- and the wiring, which is what was missing ---------------------------------------------


def test_ownership_and_co_change_reach_the_volume_report(repo: Path, tmp_path: Path) -> None:
    """`change_coupling`, `ownership` and `bus_factor` were implemented and called by nothing.

    Every function here was tested and correct, and `oxn volume` -- the surface that already
    parses this exact commit stream for hotspots -- showed none of it. A metric a user cannot
    see is a metric the project does not have, and this is the test that says so: it asserts
    the *report*, not the analysis.

    The pairing is the value. A co-change pair with no import between it is Modularity
    Violation (Mo et al., TSE 47(5), 2021); the low-expertise contributor count is what Bird
    et al. (FSE 2011) found correlating with pre-release failures at 0.86-0.93 on Vista and
    Windows 7, above size, churn and every complexity metric Microsoft collected.
    """
    from oxn.graph.indexer import Indexer
    from oxn.volume.scan import scan_volume

    with Indexer(root=repo, cache_path=tmp_path / "graph.db") as indexer:
        indexer.index()
        report = scan_volume(indexer, [repo], repo=repo)

    # `renamed.py`, not `helper.py`: the log parser follows the rename into one continuous
    # history, which `test_rename_keeps_one_continuous_history` pins. Co-change inherits that
    # for free, and asserting the pre-rename name here would have asserted a defect.
    pairs = {(pair.first, pair.second) for pair in report.coupling}
    assert ("core.py", "renamed.py") in pairs, f"the paired files must couple: {pairs}"
    assert report.bus_factor >= 1

    core = report.file_metrics["core.py"]
    assert "minor_contributors" in core and "top_share" in core
    assert 0.0 < core["top_share"] <= 1.0


def test_the_oversized_commit_still_couples_nothing(repo: Path, tmp_path: Path) -> None:
    """The filter that makes co-change mean anything, asserted through the surface.

    One reformatting commit touching 40 files would otherwise couple all 780 pairs of them to
    each other, and drown every real pair in the report.
    """
    from oxn.graph.indexer import Indexer
    from oxn.volume.scan import scan_volume

    with Indexer(root=repo, cache_path=tmp_path / "graph.db") as indexer:
        indexer.index()
        report = scan_volume(indexer, [repo], repo=repo)

    assert not [pair for pair in report.coupling if pair.first.startswith("bulk")]


def test_a_tree_with_no_history_reports_none_of_it_rather_than_zero(tmp_path: Path) -> None:
    """Absent and zero are different claims, and only one of them is true here."""
    from oxn.graph.indexer import Indexer
    from oxn.volume.scan import scan_volume

    (tmp_path / "m.py").write_text("def f():\n    return 1\n")
    with Indexer(root=tmp_path, cache_path=tmp_path / "graph.db") as indexer:
        indexer.index()
        report = scan_volume(indexer, [tmp_path], repo=tmp_path)

    assert report.coupling == [] and report.bus_factor == 0
    assert "minor_contributors" not in report.file_metrics.get("m.py", {})
