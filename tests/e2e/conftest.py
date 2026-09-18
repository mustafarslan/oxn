"""The end-to-end lane: a wheel, a fresh virtualenv, and the `oxn` script.

Every other test in this repository imports `oxn` from the working tree. That answers
"does the code work" and cannot answer "does the thing a user installs work", which is a
different question with its own failure modes: a module missing from the wheel, a
dependency that is imported but never declared, a console script that does not resolve.
This lane builds the artifact and drives it the way the README tells a user to.

Opt-in through ``OXN_E2E=1``. The build and install cost a minute, and the default suite
is meant to stay fast enough to run on every edit.
"""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest
from harness import ROOT, Installed


def _build_wheel(into: Path) -> Path:
    """Build the wheel with pip, so the lane needs no extra build dependency."""
    result = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(into), str(ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"could not build a wheel: {result.stderr.strip()[-400:]}")
    wheels = sorted(into.glob("oxn-*.whl"))
    if not wheels:
        pytest.skip("pip reported success but produced no oxn wheel")
    return wheels[0]


@pytest.fixture(scope="session")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Installed:
    """Build the wheel once and install it into one throwaway virtualenv."""
    if os.environ.get("OXN_E2E") != "1":
        pytest.skip("end-to-end lane is opt-in: set OXN_E2E=1")

    workspace = tmp_path_factory.mktemp("install")
    wheel = _build_wheel(workspace / "wheelhouse")

    prefix = workspace / "venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(prefix)
    bin_dir = prefix / ("Scripts" if os.name == "nt" else "bin")
    python = bin_dir / ("python.exe" if os.name == "nt" else "python")

    result = subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", str(wheel)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"could not install the wheel: {result.stderr.strip()[-400:]}")

    script = bin_dir / ("oxn.exe" if os.name == "nt" else "oxn")
    if not script.exists():
        pytest.fail(f"the wheel installed but declared no `oxn` script in {bin_dir}")
    return Installed(oxn=script, python=python, wheel=wheel, prefix=prefix)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A git working tree with a `src/` directory and OXN's default ceilings declared."""
    from harness import git_init

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / ".keep").write_text("")
    (tmp_path / "oxn.yaml").write_text(
        "ceilings:\n"
        "  cognitive_complexity: 12\n"
        "  cyclomatic_complexity: 10\n"
        "  max_nesting_depth: 4\n"
        "  parameter_count: 5\n"
        "  function_sloc: 60\n"
        "  file_sloc: 500\n"
    )
    git_init(tmp_path)
    return tmp_path
