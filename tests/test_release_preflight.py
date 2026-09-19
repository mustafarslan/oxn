"""The checks that run before a release, which shipped with none of their own.

`scripts/check.py --release` gained a preflight when 0.1.0 was cut: it asks whether *this
tree* is one worth publishing, before the expensive lanes spend twenty minutes proving the
code is fine. It went in untested, and the first thing it did in anger was refuse a release
for the wrong reason -- see `test_a_tag_on_head_is_not_a_problem` below.

These drive `_release_problems()` against real git repositories rather than mocks. The
function shells out to git, so a fake would be asserting that the mock matches the mock.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_check():
    """Import `scripts/check.py`, which is dev tooling rather than a package module.

    `scripts/` goes on the path first, matching `tests/test_dogfood.py`'s `load_harness`:
    the modules there import each other by plain name.
    """
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("check", ROOT / "scripts" / "check.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A committed repository declaring a version, with `check` pointed at it.

    `_declared_version` and `_git` both read `ROOT`, so the module's `ROOT` is repointed
    rather than the process's working directory -- the lane itself never chdirs.
    """
    check = _load_check()
    (tmp_path / "src" / "oxn").mkdir(parents=True)
    (tmp_path / "src" / "oxn" / "__init__.py").write_text('__version__ = "9.9.9"\n')
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "--quiet", "-m", "initial")
    monkeypatch.setattr(check, "ROOT", tmp_path)
    return check, tmp_path


def test_a_clean_untagged_tree_has_no_problems(repo) -> None:
    """The control. A preflight that objects to a releasable tree blocks every release."""
    check, _ = repo
    assert check._release_problems() == []


def test_an_undeclared_version_is_the_only_problem_reported(repo) -> None:
    """Nothing downstream means anything without it, so it short-circuits."""
    check, root = repo
    (root / "src" / "oxn" / "__init__.py").write_text("# no version here\n")
    problems = check._release_problems()
    assert problems == ["src/oxn/__init__.py declares no __version__"]


def test_an_uncommitted_change_is_reported_with_a_count(repo) -> None:
    """A release built from a dirty tree cannot be reproduced from the tag describing it."""
    check, root = repo
    (root / "stray.txt").write_text("unstaged\n")
    problems = check._release_problems()
    assert len(problems) == 1
    assert problems[0] == "uncommitted changes in 1 path(s)"


def test_a_tag_on_head_is_not_a_problem(repo) -> None:
    """The bug this test exists for.

    The first version refused whenever `v<version>` existed at all, so tagging a release and
    then verifying it was impossible -- and verifying what you just tagged is precisely the
    thing you want to do before pushing it. Hit for real while cutting 0.1.0.
    """
    check, root = repo
    _git(root, "tag", "-a", "v9.9.9", "-m", "release")
    assert check._release_problems() == [], (
        "a tag pointing at HEAD describes exactly the code being verified, and must not block"
    )


def test_a_tag_pointing_somewhere_else_is_a_problem(repo) -> None:
    """The case the rule is actually for: the tag names code other than this."""
    check, root = repo
    _git(root, "tag", "-a", "v9.9.9", "-m", "release")
    (root / "later.txt").write_text("after the tag\n")
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "moved on past the tag")

    problems = check._release_problems()
    assert len(problems) == 1
    assert "already tagged" in problems[0] and "not HEAD" in problems[0]


def test_both_problems_are_reported_together(repo) -> None:
    """Gathered rather than raised: someone who broke two things wants to see both."""
    check, root = repo
    _git(root, "tag", "-a", "v9.9.9", "-m", "release")
    (root / "later.txt").write_text("after the tag\n")
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "moved on")
    (root / "dirty.txt").write_text("and uncommitted\n")

    problems = check._release_problems()
    assert len(problems) == 2, problems
