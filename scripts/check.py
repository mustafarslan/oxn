#!/usr/bin/env python3
"""Run OXN's checks locally. This is the project's CI.

GitHub Actions is deliberately manual-only (see ``.github/workflows/ci.yml``), so this
script is where correctness is actually established. It runs the same lanes:

    python scripts/check.py               # fast lane: lint, types, unit tests
    python scripts/check.py --matrix      # the fast lane on every supported Python
    python scripts/check.py --oracle      # differential tests vs third-party tools
    python scripts/check.py --corpus      # phase exit criteria on real repositories
    python scripts/check.py --llm         # tests that call a model through Ollama
    python scripts/check.py --all         # everything

Standard library only, and it never installs anything without saying so first.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
PYTHON = VENV / "bin" / "python" if (VENV / "bin" / "python").exists() else Path(sys.executable)
SUPPORTED = ("3.10", "3.11", "3.12", "3.13", "3.14")
ORACLE_DIR = ROOT / "tests" / "oracles"

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


class Lane:
    """A named group of commands, reported as one pass/fail line."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.failures: list[str] = []
        self.started = time.perf_counter()

    def run(
        self, *command: str, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> bool:
        label = " ".join(str(part) for part in command)
        print(f"{DIM}  $ {label}{RESET}")
        merged = {**os.environ, **(env or {})}
        result = subprocess.run([str(c) for c in command], cwd=cwd or ROOT, env=merged)
        if result.returncode != 0:
            self.failures.append(label)
            return False
        return True

    def finish(self) -> bool:
        elapsed = time.perf_counter() - self.started
        if self.failures:
            print(f"{RED}FAIL{RESET} {self.name} ({elapsed:.1f}s)")
            for failure in self.failures:
                print(f"       {failure}")
            return False
        print(f"{GREEN}ok{RESET}   {self.name} ({elapsed:.1f}s)")
        return True


def fast_lane() -> bool:
    lane = Lane("fast (lint, types, unit tests)")
    lane.run(PYTHON, "-m", "ruff", "check", ".")
    lane.run(PYTHON, "-m", "ruff", "format", "--check", ".")
    lane.run(PYTHON, "-m", "mypy")
    # `llm` is excluded as well as `oracle`: the fast lane must never depend on a model
    # being reachable, which is exactly what "not oracle" alone failed to guarantee.
    lane.run(PYTHON, "-m", "pytest", "-m", "not oracle and not llm", "-q")
    return lane.finish()


def matrix_lane() -> bool:
    """The fast lane on every supported interpreter.

    This is what the GitHub matrix used to do, and it is not optional: ``enum.StrEnum``
    once slipped through because only 3.14 was tested locally.
    """
    lane = Lane(f"matrix ({', '.join(SUPPORTED)})")
    if not shutil.which("uv"):
        print(
            f"{RED}FAIL{RESET} matrix needs uv to fetch interpreters (https://docs.astral.sh/uv/)"
        )
        return False

    for version in SUPPORTED:
        env_dir = ROOT / ".venvs" / version
        if not (env_dir / "bin" / "python").exists():
            print(f"{DIM}  creating {env_dir.relative_to(ROOT)}{RESET}")
            lane.run("uv", "venv", "--python", version, "-q", str(env_dir))
        interpreter = env_dir / "bin" / "python"
        lane.run("uv", "pip", "install", "--python", str(interpreter), "-q", "-e", ".[dev]")
        lane.run(interpreter, "-m", "pytest", "-m", "not oracle and not llm", "-q")
    return lane.finish()


def oracle_lane(*, install: bool) -> bool:
    """Differential tests against tools OXN deliberately does not depend on."""
    lane = Lane("oracle (differential vs third-party tools)")
    lane.run(PYTHON, "-m", "pip", "install", "-q", "-e", ".[dev,oracle]")

    env: dict[str, str] = {}
    if shutil.which("node"):
        if install and not (ORACLE_DIR / "node_modules").exists():
            print(f"{DIM}  installing the JavaScript oracle into tests/oracles{RESET}")
            lane.run(
                "npm",
                "install",
                "--no-save",
                "--no-audit",
                "--no-fund",
                "eslint@9",
                "eslint-plugin-sonarjs@4",
                cwd=ORACLE_DIR,
            )
        if (ORACLE_DIR / "node_modules").exists():
            env["OXN_SONARJS_DIR"] = str(ORACLE_DIR)
        else:
            print(f"{DIM}  JavaScript oracle absent; pass --install to fetch it{RESET}")
    else:
        print(f"{DIM}  node not found; JavaScript oracle tests will skip{RESET}")

    pmd = ROOT / "tools" / "pmd-bin"
    if install and not pmd.exists():
        _install_pmd(lane, pmd)
    if not pmd.exists():
        print(f"{DIM}  PMD absent; duplication oracle tests will skip{RESET}")

    lane.run(PYTHON, "-m", "pytest", "-m", "oracle", "-q", env=env)
    return lane.finish()


def _install_pmd(lane: Lane, target: Path, version: str = "7.7.0") -> None:
    """Fetch PMD, whose CPD is the duplication oracle. Java only, never a runtime dep."""
    if not shutil.which("java"):
        print(f"{DIM}  java not found; skipping PMD{RESET}")
        return
    import urllib.request
    import zipfile

    url = (
        "https://github.com/pmd/pmd/releases/download/"
        f"pmd_releases%2F{version}/pmd-dist-{version}-bin.zip"
    )
    archive = target.parent / "pmd.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"{DIM}  downloading PMD {version} (~70MB){RESET}")
    try:
        urllib.request.urlretrieve(url, archive)  # noqa: S310 - pinned https release URL
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(target.parent)
        (target.parent / f"pmd-bin-{version}").rename(target)
        (target / "bin" / "pmd").chmod(0o755)
    except Exception as error:  # noqa: BLE001 - an oracle download must not fail the lane
        print(f"{DIM}  PMD download failed ({error}); its tests will skip{RESET}")
    finally:
        archive.unlink(missing_ok=True)


def llm_lane() -> bool:
    """Tests that need a language model.

    OXN's metrics are deterministic on purpose, so almost nothing here needs inference --
    these check the claims that are genuinely about a model's behaviour, chiefly that the
    explanation trail is specific enough for an agent to act on. Skips when Ollama is down
    rather than failing, so it never blocks the other lanes.
    """
    lane = Lane("llm (via Ollama)")
    host = os.environ.get("OXN_OLLAMA_HOST", "http://localhost:11434")
    model = os.environ.get("OXN_OLLAMA_MODEL", "glm-5.3:cloud")
    print(f"{DIM}  model {model} at {host}{RESET}")
    lane.run(PYTHON, "-m", "pytest", "-m", "llm", "-q", env={"OXN_OLLAMA_MODEL": model})
    return lane.finish()


def corpus_lane(*, fetch: bool) -> bool:
    """Phase exit criteria, measured on real repositories."""
    lane = Lane("corpus (phase exit criteria)")
    if fetch:
        lane.run(
            PYTHON,
            "scripts/fetch_corpora.py",
            "--name",
            "python-httpx",
            "--name",
            "typescript-nest",
        )
    lane.run(PYTHON, "-m", "pytest", "-m", "slow", "-q")
    return lane.finish()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--matrix", action="store_true", help="run on every supported Python")
    parser.add_argument(
        "--oracle", action="store_true", help="differential tests vs third-party tools"
    )
    parser.add_argument("--corpus", action="store_true", help="exit criteria on real repositories")
    parser.add_argument("--llm", action="store_true", help="tests that call a model via Ollama")
    parser.add_argument("--all", action="store_true", help="every lane")
    parser.add_argument(
        "--install", action="store_true", help="fetch oracle tools and corpora as needed"
    )
    parser.add_argument("--skip-fast", action="store_true", help="skip the fast lane")
    args = parser.parse_args(argv)

    lanes: list[bool] = []
    if not args.skip_fast:
        lanes.append(fast_lane())
    if args.matrix or args.all:
        lanes.append(matrix_lane())
    if args.oracle or args.all:
        lanes.append(oracle_lane(install=args.install or args.all))
    if args.corpus or args.all:
        lanes.append(corpus_lane(fetch=args.install))
    if args.llm or args.all:
        lanes.append(llm_lane())

    print()
    if all(lanes):
        print(f"{BOLD}{GREEN}all lanes passed{RESET}")
        return 0
    print(f"{BOLD}{RED}one or more lanes failed{RESET}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
