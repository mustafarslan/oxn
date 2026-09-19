"""Every phase label in this repository has somewhere to look it up.

OXN was built in increments `P0`-`P12`, and the documents that defined them were removed from
the repository and from its history. The labels could not go with them: 44 public commit
subjects carry them, and 17 of those subjects are *pinned query text* in
`benchmarks/retrieval-labels-oxn.json`, so rewording them would move a recorded measurement.

`docs/phases.md` is therefore the definition, and these two tests are what keep it one. The
first says every label in the tree has a row. The second says no label reaches a user, which
is a stronger claim and the one worth having: a reader of the source can open `phases.md`, but
somebody running `oxn calibration` should not have to.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PHASES = ROOT / "docs" / "phases.md"

#: `P0`-`P12`, optionally `.5`. Bounded at 12 on purpose: this codebase also writes percentile
#: notation as `P79`, `P97.6`, `P100`, and those are numbers rather than phases. The bound is
#: what separates the two, and it is not perfect -- a percentile written `P5` would be read as
#: a phase. Nothing distinguishes them by pattern, so the bound is the whole defence; the
#: observed percentiles all sit at 48 and above, which is why it holds in practice. A
#: false match costs a spurious row lookup in `docs/phases.md`, which is a cheap failure.
LABEL = re.compile(r"\bP(?:1[0-2]|[0-9])(?:\.5)?\b")

#: `benchmarks/` holds vendored third-party corpora -- 341 unrelated `P1`/`P2` hits in other
#: people's source -- plus the pinned benchmark data that must not be edited at all.
SEARCHED = ("src", "scripts", "tests", "docs", ".github")


def _tracked_text_files() -> list[Path]:
    found: list[Path] = []
    for directory in SEARCHED:
        for path in (ROOT / directory).rglob("*"):
            noise = {"__pycache__", "node_modules"} & set(path.parts)
            wanted = path.suffix in {".py", ".md", ".yml", ".yaml", ".toml"}
            if wanted and not noise and path.is_file():
                found.append(path)
    found.append(ROOT / "README.md")
    found.append(ROOT / "oxn.yaml")
    return found


def test_every_phase_label_has_a_row_in_the_phase_record() -> None:
    """A label nobody can look up is the thing this file exists to prevent recurring."""
    record = PHASES.read_text()
    defined = set(LABEL.findall(record))
    assert defined, "docs/phases.md defines no phases at all"

    used: dict[str, str] = {}
    for path in _tracked_text_files():
        if path == PHASES:
            continue
        for label in LABEL.findall(path.read_text(encoding="utf-8", errors="ignore")):
            used.setdefault(label, str(path.relative_to(ROOT)))

    undefined = {label: where for label, where in used.items() if label not in defined}
    assert not undefined, (
        "phase labels used in the tree with no row in docs/phases.md: "
        + ", ".join(f"{label} (first seen {where})" for label, where in sorted(undefined.items()))
    )


def _calibration_strings() -> dict[str, str]:
    """What `oxn calibration` prints, and what its `--json` payload carries."""
    from oxn import calibration

    found = {"calibration.summary()": json.dumps(calibration.summary())}
    for parameter in calibration.parameters():
        found[f"{parameter.name}.provenance"] = parameter.provenance
        found[f"{parameter.name}.fit_when"] = parameter.fit_when or ""
    return found


def _command_docstrings() -> dict[str, str]:
    """Typer prints a command's *whole* docstring for `--help`, not just its first line."""
    from oxn import _app

    found: dict[str, str] = {}
    for command in _app.app.registered_commands:
        callback = command.callback
        if callback is not None and callback.__doc__:
            found[f"oxn {callback.__name__} --help"] = callback.__doc__
    return found


def _argparse_help(tree: ast.Module) -> dict[int, str]:
    """Every `help=` string literal in a parsed script, keyed by line."""
    found: dict[int, str] = {}
    for node in ast.walk(tree):
        is_help = isinstance(node, ast.keyword) and node.arg == "help"
        literal = is_help and isinstance(node.value, ast.Constant)
        if literal and isinstance(node.value.value, str):
            found[node.lineno] = node.value.value
    return found


def _script_strings() -> dict[str, str]:
    """Scripts passing `description=__doc__` print the module docstring on `--help`."""
    found: dict[str, str] = {}
    for script in sorted((ROOT / "scripts").glob("*.py")):
        source = script.read_text()
        if "description=__doc__" not in source:
            continue
        tree = ast.parse(source)
        found[f"{script.name} --help"] = ast.get_docstring(tree) or ""
        for line, text in _argparse_help(tree).items():
            found[f"{script.name} help= (line {line})"] = text
    return found


def _printed_strings() -> dict[str, str]:
    """Everything the installed tool can put in front of a user, by where it comes from."""
    return {**_calibration_strings(), **_command_docstrings(), **_script_strings()}


def test_nothing_printed_to_a_user_carries_a_phase_label() -> None:
    """The acceptance check for the substitution pass.

    A phase label in a docstring is a note to a contributor, who can open `docs/phases.md`.
    A phase label in `oxn calibration`'s output is an undefined token in a report about why a
    threshold has the value it does, shown to somebody who has never read this repository.
    """
    offenders = {
        where: sorted(set(LABEL.findall(text)))
        for where, text in _printed_strings().items()
        if LABEL.search(text)
    }
    assert not offenders, "phase labels reach users through: " + "; ".join(
        f"{where} {labels}" for where, labels in sorted(offenders.items())
    )


def _tracked_paths() -> set[str]:
    """What `git ls-files` returns, which is what somebody who cloned the repo actually has."""
    import subprocess

    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return set(result.stdout.split())


def test_nothing_printed_to_a_user_cites_a_file_they_do_not_have() -> None:
    """A citation to a path the reader cannot open is the roadmap problem in miniature.

    `benchmarks/dogfood-log.jsonl` is the case this was written for: 2.3 MB of repair
    trajectories, gitignored because most of it is raw model replies, and named in three
    strings `oxn calibration` prints as the evidence behind a threshold. Someone who ran
    `pip install oxn` was being pointed at a file that has never been distributed.

    The rule is deliberately narrow -- a *path* in printed text must be one the reader has.
    Describing the file in prose is fine and is what those three strings do now.
    """
    tracked = _tracked_paths()
    pattern = re.compile(r"\b(?:benchmarks|docs|src|tests|scripts)/[\w./-]+\.\w+")

    missing: dict[str, set[str]] = {}
    for where, text in _printed_strings().items():
        absent = {cited for cited in pattern.findall(text) if cited not in tracked}
        if absent:
            missing[where] = absent

    assert not missing, "printed text cites files that are not in the repository: " + "; ".join(
        f"{where} -> {sorted(paths)}" for where, paths in sorted(missing.items())
    )
