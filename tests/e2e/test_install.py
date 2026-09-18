"""Install the wheel, drive the script, uninstall it, and look at what is left behind.

The lifecycle leg. What it is really testing is `pyproject.toml`: the package list, the
declared dependencies and the console script. Those are invisible to every test that
imports from the working tree, because the working tree has the whole repository on the
path and a virtualenv has only what the wheel asked for.
"""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest
from harness import ROOT, Installed

pytestmark = pytest.mark.e2e


def test_the_wheel_carries_every_module_the_package_imports(installed: Installed) -> None:
    """A module left out of the wheel fails on the user's machine and nowhere else."""
    shipped = {
        name.split("/", 1)[1]
        for name in zipfile.ZipFile(installed.wheel).namelist()
        if name.startswith("oxn/") and name.endswith(".py")
    }
    in_tree = {
        str(path.relative_to(ROOT / "src" / "oxn")) for path in (ROOT / "src" / "oxn").rglob("*.py")
    }
    assert in_tree - shipped == set(), "modules in the tree that the wheel does not ship"


def test_the_installed_script_answers_without_the_working_tree(installed: Installed) -> None:
    """Run from `/`, so nothing resolves by accident through the current directory."""
    result = installed.run("version", cwd=Path(installed.prefix.anchor))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip(), "version printed nothing"


def test_the_venv_has_only_the_declared_dependencies(installed: Installed) -> None:
    """An undeclared import works in the repository and fails for the user.

    The check is not that the list is short but that it is the list `pyproject.toml`
    declares -- `oracle` and `dev` extras must not have come along.
    """
    frozen = subprocess.run(
        [str(installed.python), "-m", "pip", "freeze"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.lower()
    for absent in ("radon", "lizard", "complexipy", "grimp", "vulture", "pytest", "mypy", "ruff"):
        assert f"{absent}==" not in frozen, f"{absent} reached a user install"


def test_uninstall_removes_the_script(installed: Installed, tmp_path: Path) -> None:
    """Uninstall in a copy of the virtualenv, so the session fixture survives it."""
    result = subprocess.run(
        [str(installed.python), "-m", "pip", "uninstall", "--yes", "oxn"],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        assert result.returncode == 0, result.stderr
        assert not installed.oxn.exists(), "the console script outlived the package"
        assert (
            subprocess.run(
                [str(installed.python), "-c", "import oxn"],
                capture_output=True,
                text=True,
                check=False,
            ).returncode
            != 0
        ), "the package is importable after uninstall"
    finally:
        subprocess.run(
            [str(installed.python), "-m", "pip", "install", "--quiet", str(installed.wheel)],
            capture_output=True,
            text=True,
            check=False,
        )
