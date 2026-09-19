"""The other artifact. `pip install oxn` may resolve to a source distribution, not a wheel.

Everything in `test_install.py` drives a wheel, which is what `pip` prefers and therefore
what almost every user gets. But the sdist is published by the same `hatch build`, is what
`pip install --no-binary` and most distribution packagers use, and is built from a
*different* section of `pyproject.toml` -- `tool.hatch.build.targets.sdist`, whose
`include` list names four directories the wheel never touches.

That list is the hazard. `benchmarks/` is 2 GB on a machine that has run
`scripts/fetch_corpora.py`, and it is named in `include`; the only thing keeping other
people's source out of OXN's release is hatchling honouring `.gitignore`. That is a
default, not a declaration, and a default nobody asserts is a default that can change under
you in a minor release of the build backend.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from harness import ROOT, Installed

pytestmark = pytest.mark.e2e

#: Paths that must never reach a release. Fetched third-party corpora, caches written by
#: the oracle tools, and the virtualenvs the matrix lane builds -- all gitignored, all
#: sitting inside directories `tool.hatch.build.targets.sdist` names explicitly.
MUST_NOT_SHIP = (
    "benchmarks/corpora/",
    ".venv/",
    ".venvs/",
    ".git/",
    "tests/oracles/node_modules/",
    # The one that was actually getting through on 2026-09-19: complexipy writes it
    # during the oracle lane and nothing ignored it, so hatchling packed it.
    ".complexipy_cache/",
    # OXN's own analysis cache. A `src/.oxn/cache/graph.db` was committed on 2026-09-04
    # and shipped in every artifact built after it, because `.gitignore` anchored the rule
    # at the root and the file was tracked anyway -- and gitignore does not apply to
    # tracked files. Found in the v0.1.0 release scan as the largest file in the archive.
    ".oxn/cache/",
    ".oxn/index/",
)

#: No single file in a release may be larger than this. The path list above only catches
#: leaks someone predicted; this catches the shape instead -- a stray binary, a committed
#: database, a vendored blob -- whatever directory it arrives from. The largest legitimate
#: files are prose and recorded measurements: docs/metrics.md at 120 KB and
#: benchmarks/retrieval-corpus-oxn.json at 108 KB. 300 KB leaves those room and still
#: rejects the 984 KB cache that prompted it.
LARGEST_FILE_BYTES = 300 * 1024

#: A release that is mostly other people's code is a packaging bug, and a size ceiling is
#: the one assertion that catches every future variant of it at once. The sdist measured
#: 1.1 MB on 2026-09-19; 25 MB leaves room to grow and still fails the moment a corpus,
#: a node_modules or a PMD download starts riding along.
SDIST_CEILING_BYTES = 25 * 1024 * 1024


def _builders(into: Path) -> tuple[list[str], ...]:
    """The ways to get an sdist, best first.

    `pip wheel` builds the conftest's wheel and has no sdist equivalent, so this lane needs
    a real build frontend. `build` is in the `dev` extra for that reason; `uv` is tried
    after it because the matrix lane already requires uv, so a developer who can run
    `--matrix` can run this without installing anything further.
    """
    return (
        [sys.executable, "-m", "build", "--sdist", "--outdir", str(into), str(ROOT)],
        ["uv", "build", "--sdist", "--out-dir", str(into), str(ROOT)],
    )


@pytest.fixture(scope="session")
def sdist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the source distribution with the backend `pyproject.toml` declares."""
    into = tmp_path_factory.mktemp("sdist")
    attempts: list[str] = []
    for command in _builders(into):
        if command[0] == "uv" and not shutil.which("uv"):
            continue
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            break
        attempts.append(f"{command[0]}: {result.stderr.strip()[-200:]}")
    built = sorted(into.glob("oxn-*.tar.gz"))
    if not built:
        pytest.skip(f"no sdist builder available ({'; '.join(attempts) or 'none tried'})")
    return built[0]


def _members(archive: Path) -> list[str]:
    """Archive paths with the `oxn-<version>/` prefix stripped."""
    with tarfile.open(archive) as bundle:
        return [name.split("/", 1)[1] for name in bundle.getnames() if "/" in name]


def _under(path: str, forbidden: str) -> bool:
    """Is `path` inside a `forbidden` directory at *any* depth?

    Anchoring this at the root is the bug that let the cache through in the first place:
    `.gitignore` said `.oxn/cache/`, which matches only at the top level, and the file was
    at `src/.oxn/cache/graph.db`. The first version of this very check repeated the mistake
    with `startswith`, and passed on the leak it was written for.
    """
    return path.startswith(forbidden) or f"/{forbidden}" in f"/{path}"


def test_the_sdist_ships_none_of_the_fetched_trees(sdist: Path) -> None:
    """`include` names `benchmarks`, and `benchmarks/corpora` is 2 GB of other people's code."""
    shipped = _members(sdist)
    for forbidden in MUST_NOT_SHIP:
        leaked = [name for name in shipped if _under(name, forbidden)]
        assert not leaked, f"{forbidden} reached the sdist ({len(leaked)} paths, e.g. {leaked[0]})"


def test_the_sdist_stays_the_size_of_a_project_rather_than_a_corpus(sdist: Path) -> None:
    """The ceiling that catches the next leak, whatever directory it comes from."""
    size = sdist.stat().st_size
    assert size < SDIST_CEILING_BYTES, (
        f"the sdist is {size / 1024 / 1024:.1f} MB, over the {SDIST_CEILING_BYTES // 1024**2} MB "
        "ceiling -- something large is riding along"
    )


def test_the_sdist_carries_what_it_claims_to(sdist: Path) -> None:
    """`include` promises the tests, the docs and the scripts; a promise nobody checks rots."""
    shipped = _members(sdist)
    for promised in ("src/oxn/__init__.py", "tests/conftest.py", "docs/metrics.md", "README.md"):
        assert promised in shipped, (
            f"`include` names it and the sdist does not carry it: {promised}"
        )


def test_no_single_file_in_the_release_is_a_blob(sdist: Path) -> None:
    """The guard for the leak nobody predicted.

    `MUST_NOT_SHIP` is a list of paths someone thought of. This asserts the property those
    paths are a proxy for: a source distribution is text, and anything much larger than the
    longest document in it is something that got in by accident.
    """
    with tarfile.open(sdist) as bundle:
        oversized = {
            member.name.split("/", 1)[1]: member.size
            for member in bundle.getmembers()
            if member.isfile() and member.size > LARGEST_FILE_BYTES
        }
    assert not oversized, (
        "files over the "
        f"{LARGEST_FILE_BYTES // 1024} KB per-file ceiling reached the sdist: "
        + ", ".join(f"{name} ({size // 1024} KB)" for name, size in sorted(oversized.items()))
    )


def test_the_sdist_installs_and_answers(sdist: Path, tmp_path: Path) -> None:
    """The question the wheel lane cannot answer: does a from-source install work at all?

    This builds the package a second time, from the archive rather than the checkout, which
    is the step that catches a `pyproject.toml` referencing a file the sdist left out.
    """
    import venv

    prefix = tmp_path / "from-sdist"
    venv.EnvBuilder(with_pip=True, clear=True).create(prefix)
    python = prefix / "bin" / "python"

    install = subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", str(sdist)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert install.returncode == 0, f"the sdist does not install: {install.stderr[-600:]}"

    script = prefix / "bin" / "oxn"
    assert script.exists(), "installed from sdist and declared no `oxn` script"
    answered = subprocess.run([str(script), "version"], capture_output=True, text=True, check=False)
    assert answered.returncode == 0, answered.stderr
    assert answered.stdout.strip(), "version printed nothing"


def test_both_artifacts_report_the_same_version(sdist: Path, installed: Installed) -> None:
    """A wheel and an sdist built from one tree that disagree is a release nobody can pin.

    Both are compared against `src/oxn/__init__.py`, which `tool.hatch.version` reads, so a
    failure here names which of the three drifted.
    """
    declared = {}
    for line in (ROOT / "src" / "oxn" / "__init__.py").read_text().splitlines():
        if line.startswith("__version__"):
            declared["source"] = line.split("=", 1)[1].strip().strip('"').strip("'")
    assert declared, "no `__version__` in src/oxn/__init__.py"

    assert sdist.name == f"oxn-{declared['source']}.tar.gz", (
        f"the sdist is named {sdist.name} and the source says {declared['source']}"
    )
    reported = installed.run("version").stdout.strip()
    assert declared["source"] in reported, (
        f"the installed script says {reported!r} and the source says {declared['source']}"
    )
