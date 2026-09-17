"""Clone recall against PMD-CPD across five languages, and the yield curve behind `50`.

    python scripts/check.py --oracle --install      # PMD, once
    python scripts/measure_duplication.py

**This is the measurement `DUPLICATION_MIN_TOKENS.fit_when` asks for.** That parameter shipped
at `observations=1`: recall was measured on httpx, one corpus in one language, and its
`fit_when` said the number could not be fitted "until clone recall is measured across more
than one corpus". This is that, and it is deliberately two measurements rather than one,
because the provenance makes two claims and only the first was ever checked:

* **parity** -- OXN at 50 finds what PMD-CPD at 50 finds. A claim about the detector.
* **the value** -- 50 rather than CPD's default 100. Recall at 50 says nothing whatever about
  this; it compares two tools at the same setting. What speaks to it is the *yield curve*,
  how much duplication each tool reports at 30, 50, 75 and 100, which is why that table is
  here too.

Both sides analyse an identical file list. This matters more than it sounds: PMD walks
`--dir` itself while OXN goes through `iter_source_files`, so any file one sees and the other
does not scores as a recall miss that is really a disagreement about what a source file is.
`analysed` decides once -- files of the language in question that OXN can parse -- and PMD is
handed exactly those paths via `--file-list`.

**Rust is fetched, analysed by OXN, and absent from every table below.** PMD-CPD 7.7.0 ships
no Rust lexer, so there is no oracle to compare against rather than a result worth omitting.
Saying so is the point: five of the six launch languages are checked here and the sixth is
unchecked, which is a different statement from six.
"""

from __future__ import annotations

import csv
import io
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oxn.thresholds import DUPLICATION_MIN_LINES

ROOT = Path(__file__).resolve().parent.parent
CORPORA = ROOT / "benchmarks" / "corpora"
PMD = Path(os.environ.get("OXN_PMD_DIR", ROOT / "tools" / "pmd-bin")) / "bin" / "pmd"

#: corpus directory -> (the profile OXN selects, the lexer `pmd cpd -l` wants). The two names
#: differ for JavaScript, where CPD calls the language `ecmascript`.
CORPUS_LANGUAGES: dict[str, tuple[str, str]] = {
    "python-httpx": ("python", "python"),
    "java-spring-petclinic": ("java", "java"),
    "go-kit": ("go", "go"),
    "javascript-eslint": ("javascript", "ecmascript"),
    "typescript-nest": ("typescript", "typescript"),
}

#: The band the question lives in. 100 is CPD's default and the value this parameter argues
#: against; 30 is below anything either tool would call a clone worth reporting.
THRESHOLDS = (30, 50, 75, 100)


def analysed(directory: Path, language: str) -> tuple[list[str], dict[str, Any]]:
    """The file list both tools get, and OXN's parsed nodes for it.

    Files of `language` that OXN can parse. Unparseable files are dropped from *both* sides
    rather than from OXN's alone, which is the only way the recall denominator stays honest --
    eslint ships deliberately malformed fixtures, and a lexer error on one side and a parse
    error on the other are not the same file being skipped.
    """
    from oxn.graph.sources import iter_source_files
    from oxn.languages import get_parser
    from oxn.profiles import profile_for_path

    paths: list[str] = []
    parsed: dict[str, Any] = {}
    for path in iter_source_files([directory]):
        profile = profile_for_path(str(path))
        if profile is None or profile.name != language:
            continue
        tree = get_parser(profile.grammar).parse(path.read_bytes())
        if tree.root_node.has_error:
            continue
        relative = path.resolve().relative_to(directory).as_posix()
        paths.append(relative)
        parsed[relative] = (tree, profile)
    return sorted(paths), parsed


def oxn_clone_lines(
    directory: Path, parsed: dict[str, Any], min_tokens: int, min_lines: int
) -> set[tuple[str, int]]:
    """(path, line) for every line OXN reports as duplicated, at `min_tokens`."""
    from oxn.volume.clones import file_windows, find_clones

    windows = {
        relative: file_windows(relative, tree.root_node, profile, min_tokens=min_tokens)
        for relative, (tree, profile) in parsed.items()
    }
    report = find_clones(windows, min_tokens=min_tokens, min_lines=min_lines)
    return {
        (clone.path, line)
        for klass in report.clone_classes
        for clone in klass.occurrences
        for line in clone.lines
    }


def pmd_clone_lines(
    directory: Path, language: str, paths: list[str], min_tokens: int
) -> set[tuple[str, int]]:
    """(path, line) for every line PMD-CPD reports as duplicated, over exactly `paths`."""
    listing = directory / ".oxn-cpd-filelist"
    listing.write_text("\n".join(str(directory / path) for path in paths))
    try:
        result = subprocess.run(
            [str(PMD), "cpd", "--minimum-tokens", str(min_tokens), "--language", language,
             "--file-list", str(listing), "--format", "csv"],
            capture_output=True,
            text=True,
        )
    finally:
        listing.unlink(missing_ok=True)
    return _parse_cpd_csv(result.stdout, directory)


def _parse_cpd_csv(output: str, directory: Path) -> set[tuple[str, int]]:
    """CPD's CSV is `lines,tokens,occurrences,(start,file)*` -- expand each span to its lines."""
    lines: set[tuple[str, int]] = set()
    for row in csv.reader(io.StringIO(output)):
        if not row or row[0] == "lines":
            continue
        span = int(row[0])
        rest = row[3:]
        for index in range(0, len(rest) - 1, 2):
            start = int(rest[index])
            path = Path(rest[index + 1]).relative_to(directory).as_posix()
            lines.update((path, start + offset) for offset in range(span))
    return lines


@dataclass(frozen=True)
class Corpus:
    """One corpus parsed once, so the four thresholds do not re-parse 1,800 files each."""

    name: str
    directory: Path
    lexer: str
    paths: list[str]
    parsed: dict[str, Any]


@dataclass(frozen=True)
class Measured:
    """What both tools found at one threshold, and how far apart they are."""

    corpus: str
    min_tokens: int
    pmd_lines: int
    oxn_lines: int
    recall: float
    line_jaccard: float
    file_jaccard: float


def _measure_at(corpus: Corpus, min_tokens: int) -> Measured:
    ours = oxn_clone_lines(corpus.directory, corpus.parsed, min_tokens, DUPLICATION_MIN_LINES)
    theirs = pmd_clone_lines(corpus.directory, corpus.lexer, corpus.paths, min_tokens)
    our_files, their_files = {p for p, _ in ours}, {p for p, _ in theirs}
    return Measured(
        corpus=corpus.name,
        min_tokens=min_tokens,
        pmd_lines=len(theirs),
        oxn_lines=len(ours),
        recall=len(ours & theirs) / len(theirs) if theirs else float("nan"),
        line_jaccard=len(ours & theirs) / len(ours | theirs) if ours | theirs else float("nan"),
        file_jaccard=(
            len(our_files & their_files) / len(our_files | their_files)
            if our_files | their_files
            else float("nan")
        ),
    )


def load(name: str) -> Corpus:
    language, lexer = CORPUS_LANGUAGES[name]
    directory = (CORPORA / name).resolve()
    paths, parsed = analysed(directory, language)
    return Corpus(name=name, directory=directory, lexer=lexer, paths=paths, parsed=parsed)


def main() -> int:
    header = f"{'corpus':<24}{'files':>6}{'tok':>5}{'PMD':>8}{'OXN':>8}{'recall':>8}{'lineJ':>7}{'fileJ':>7}"
    print(header)
    print("-" * len(header))
    for name in CORPUS_LANGUAGES:
        corpus = load(name)
        for min_tokens in THRESHOLDS:
            row = _measure_at(corpus, min_tokens)
            print(
                f"{name:<24}{len(corpus.paths):>6}{row.min_tokens:>5}{row.pmd_lines:>8}"
                f"{row.oxn_lines:>8}{row.recall:>8.3f}{row.line_jaccard:>7.3f}{row.file_jaccard:>7.3f}"
            )
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
