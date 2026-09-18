"""What the end-to-end lane drives, and how a fixture becomes a file on disk.

Kept beside the lane rather than in `conftest.py` so the test modules can import it by
name, which is the convention `tests/oracle_support.py` already sets in this repository.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"


@dataclass(frozen=True)
class Installed:
    """An `oxn` installed from a wheel into a virtualenv of its own."""

    oxn: Path
    python: Path
    wheel: Path
    prefix: Path

    def run(
        self,
        *args: str,
        cwd: Path | None = None,
        stdin: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Invoke the installed script, never the working tree."""
        environment = {**os.environ, **(env or {})}
        environment["PATH"] = f"{self.oxn.parent}{os.pathsep}{environment['PATH']}"
        return subprocess.run(
            [str(self.oxn), *args],
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )


def plant(source: Path, into: Path, *, name: str | None = None) -> Path:
    """Copy a `.txt` fixture to its real extension.

    Fixtures are stored suffixed so that nothing in this repository treats them as source:
    not ruff, not mypy, and not OXN's own gate -- several of them are deliberately over a
    ceiling, and one of them does not parse.
    """
    target = into / (name or source.name.removesuffix(".txt"))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return target


#: The six launch languages, and the fixture that carries one shape into each of them.
SAME_SHAPE = {
    "python": FIXTURES / "same_shape" / "sample.py.txt",
    "typescript": FIXTURES / "same_shape" / "sample.ts.txt",
    "javascript": FIXTURES / "same_shape" / "sample.js.txt",
    "go": FIXTURES / "same_shape" / "sample.go.txt",
    "rust": FIXTURES / "same_shape" / "sample.rs.txt",
    "java": FIXTURES / "same_shape" / "Sample.java.txt",
}

#: What `classify` measures in every one of those six grammars. Hand-derived and then
#: confirmed against each grammar independently; the per-metric judgements behind the
#: numbers are pinned by `tests/test_polyglot.py` and anchored to radon/complexipy in the
#: oracle lane. Cognitive: `for` +1, `if` +2, `if` +3, `else` +1, `while` +1, `if` +1.
SAME_SHAPE_METRICS = {
    "cognitive_complexity": 9.0,
    "cyclomatic_complexity": 6.0,
    "max_nesting_depth": 3.0,
    "parameter_count": 2.0,
    "exit_points": 2.0,
}


def git_init(root: Path) -> None:
    """A real repository, so `git status` can answer what the lifecycle left behind."""
    for command in (
        ["git", "init", "--quiet"],
        ["git", "config", "user.email", "e2e@example.invalid"],
        ["git", "config", "user.name", "e2e"],
        ["git", "add", "-A"],
        ["git", "commit", "--quiet", "-m", "fixture"],
    ):
        subprocess.run(command, cwd=root, capture_output=True, text=True, check=True)
