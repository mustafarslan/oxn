"""The CK suite over one shape, in every language that spells that shape the same way.

`docs/metrics.md` makes agreement the acceptance gate: identical logic transliterated
between languages must score identically, and where it does not, one of the two is wrong.
The inheritance half of the suite has that gate in `tests/test_inheritance.py` and the
cohesion half in `tests/test_cross_language_cohesion.py`. **Coupling had none** -- CBO and
RFC were measured per language and never against each other.

Three classes: `Base` with one method, `Repo` with two that share nothing, and
`Service extends Base` holding a `Repo` and calling both of its methods. That fixes every
number in the suite at once, which is the point -- a change that moves one of them in one
language has to move it everywhere or fail here.

Go and Rust are absent, and deliberately. Neither writes this shape: Go's interfaces are
satisfied structurally so there is no edge to find, and Rust implements rather than extends.
Both are covered on their own terms in the two files above; inventing an `extends` for them
here would make the table agree by making it wrong.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ck_agreement"

_FILE = {
    "python": "m.py",
    "typescript": "m.ts",
    "javascript": "m.js",
    "java": "M.java",
}

#: class -> (DIT, NOC, CBO, RFC, WMC, LCOM4). One row, four languages.
EXPECTED = {
    "Base": (0, 1, 0, 1, 1, 1),
    "Repo": (0, 0, 0, 2, 2, 2),
    "Service": (1, 0, 1, 2, 2, 1),
}


def _classes(language: str, tmp_path: Path) -> dict[str, tuple[int, ...]]:
    """`oxn classes --json` in a directory holding only this language's file.

    Through the CLI rather than the library, because that is where the numbers a user reads
    come from -- and because `oxn classes` and the gate compute NOM by different paths, which
    is the split `tests/test_impl_block_evasion.py` exists about.
    """
    name = _FILE[language]
    (tmp_path / name).write_text((FIXTURES / f"{name}.txt").read_text())
    finished = subprocess.run(
        [sys.executable, "-m", "oxn", "classes", "--json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert finished.returncode == 0, finished.stderr
    payload = json.loads(finished.stdout)
    return {
        row["class"]: (row["dit"], row["noc"], row["cbo"], row["rfc"], row["wmc"], row["lcom4"])
        for row in payload["classes"]
    }


@pytest.mark.parametrize("language", sorted(_FILE))
def test_the_whole_ck_suite_agrees_across_languages(language: str, tmp_path: Path) -> None:
    assert _classes(language, tmp_path) == EXPECTED, language
