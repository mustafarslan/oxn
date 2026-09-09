"""One type, two files, and the count that must not depend on which file you read.

Go declares a method at file scope carrying a receiver, and Rust puts a type's methods in
`impl` blocks. Either language may spread one type across a package, and `oxn classes`
measured per file, so `Counter` came back **twice** -- two methods beside the struct and
three in its sibling -- with neither row being the type.

The split count was not the worst of it. The sibling's methods read fields declared in the
*other* file, which its own tree never sees, so those reads were dropped as unresolved and
the two halves looked like two types sharing nothing: LCOM4 read 3 where the type has 2.
That is the shape of an answer that is confidently wrong rather than missing.

The gate has joined at package scope since `indexer.aggregate_classes`, so before this the
two paths disagreed on real multi-file code -- and a type with two method counts has one that
a ceiling gets compared against. `tests/test_cross_language_cohesion.py` holds them equal.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "split_across_files"

#: `Counter` has five methods over two fields: three touch `n`, two touch `tag`.
GO = ("Counter", 5, 2)
#: `Buf` has five methods over one field, so it is a single component.
RUST = ("Buf", 5, 1)


@pytest.fixture
def package(tmp_path, monkeypatch):
    """A directory holding one language's pair of files, and nothing else."""

    def build(suffix: str) -> Path:
        for fixture in FIXTURES.glob(f"*.{suffix}.txt"):
            shutil.copyfile(fixture, tmp_path / fixture.name.removesuffix(".txt"))
        monkeypatch.chdir(tmp_path)
        return tmp_path

    return build


def _classes(_project: Path, capsys) -> list[dict]:
    from oxn.report import run_classes

    payload = run_classes(["."])
    capsys.readouterr()  # `run_*` writes its JSON to stdout; the return value is the subject
    return payload["classes"]


@pytest.mark.parametrize(("suffix", "expected"), (("go", GO), ("rs", RUST)))
def test_a_type_split_across_files_is_reported_once_and_whole(
    package, capsys, suffix: str, expected: tuple[str, int, int]
) -> None:
    name, nom, lcom4 = expected
    rows = [row for row in _classes(package(suffix), capsys) if row["class"] == name]
    assert len(rows) == 1, f"{name} appears {len(rows)} times: {[r['path'] for r in rows]}"
    assert rows[0]["nom"] == nom, f"{name}: nom {rows[0]['nom']}"
    assert rows[0]["lcom4"] == lcom4, f"{name}: lcom4 {rows[0]['lcom4']}"


@pytest.mark.parametrize("suffix", ("go", "rs"))
def test_the_type_is_reported_against_the_file_that_declares_it(
    package, capsys, suffix: str
) -> None:
    """A file holding three methods and none of the state is not where a reader would look."""
    rows = {row["class"]: row["path"] for row in _classes(package(suffix), capsys)}
    assert rows and all(path.endswith(f"type.{suffix}") for path in rows.values()), rows


@pytest.mark.parametrize("suffix", ("go", "rs"))
def test_a_cross_file_field_read_is_not_left_unresolved(package, capsys, suffix: str) -> None:
    """The half that made the split answer wrong rather than merely incomplete.

    A sibling file's `c.n` is a read of a field the type does declare. Dropped, it made the
    two halves incohesive; recovered, the exactness stamp has nothing to qualify.
    """
    rows = _classes(package(suffix), capsys)
    assert rows and all(row["exactness"] == "EXACT" for row in rows), rows
