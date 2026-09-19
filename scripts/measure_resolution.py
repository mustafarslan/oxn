"""Regenerate every resolution figure OXN publishes, for all six languages.

    python scripts/measure_resolution.py            # all six, reusing cached SCIP indexes
    python scripts/measure_resolution.py go rust    # just these

**This exists because the figures kept going stale in a particular way.** The per-language
precision, recall and call-coverage numbers live in `tests/test_oracle_scip.py` docstrings and
in the resolution table, and the tests assert *floors* rather than the figures -- so a change that
moves a number by five points passes every test and leaves six documents claiming the old one.
It happened three times in one week: the receiver fallback fell and the headline coverage was
corrected while the per-cause split beneath it was not; the split's explanation outlived the
defect it described; and Go's row drifted 7 points of confident recall with nothing failing.

An ad-hoc script written each time is what made that possible. This one is committed, so
re-measuring is a command rather than a half-hour of reconstruction, and the output is meant to
be pasted into the docstring it contradicts.

Needs the SCIP indexers on PATH (`oxn doctor` says which are missing) and the corpora fetched
(`python scripts/fetch_corpora.py`). Missing either, it says so for that language and continues.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPORA: dict[str, str] = {
    "python": "python-httpx",
    "go": "go-kit",
    "rust": "rust-ripgrep",
    "typescript": "typescript-nest",
    "javascript": "javascript-eslint",
    "java": "java-spring-petclinic",
}
#: Where built indexes are kept between runs. A SCIP index is minutes of work and deterministic
#: for a pinned corpus, so rebuilding it every time is the reason nobody re-measures.
CACHE = ROOT / "benchmarks" / ".scip-cache"


def build_index(language: str, corpus: Path, index: Path) -> str | None:
    """Build the SCIP index, or return why not. Returns ``None`` on success."""
    if index.exists():
        return None
    CACHE.mkdir(parents=True, exist_ok=True)
    from oxn.scip.runner import INDEXERS, run_indexer

    if language == "java":
        # petclinic declares both Maven and Gradle, so `scip-java` refuses to choose and
        # `scip.runner` refuses to choose for it -- guessing there runs the wrong build for ten
        # minutes. The oracle test passes `--build-tool=maven` explicitly and so does this.
        binary = shutil.which("scip-java")
        if binary is None:
            return "scip-java not on PATH"
        subprocess.run(  # noqa: S603
            [binary, "index", "--build-tool=maven", "--output", str(index)],
            cwd=corpus,
            check=True,
            capture_output=True,
            # The same budget the registry gives Java; this path calls `scip-java` directly
            # only because petclinic declares two build tools.
            timeout=INDEXERS["java"].timeout,
        )
        return None

    # No timeout here: `Indexer.timeout` carries each language's budget, and a script
    # repeating it is how four call sites came to hold the same knowledge separately.
    run_indexer(language, corpus, index)
    return None


def measure(language: str, corpus: Path, index: Path) -> None:
    """Print the published figures for one language, exactly as the docstrings state them."""
    from oxn.graph.indexer import Indexer
    from oxn.resolve.measure import measure_corpus
    from oxn.scip.ingest import ingest_index

    accuracy = measure_corpus(corpus, index, language=language)
    confident = accuracy.confident_answered or 1
    print(
        f"  L0/L1  {accuracy.confident_correct / confident:.1%} precision when certain, "
        f"{confident / (accuracy.graded_call_sites or 1):.1%} confident recall, "
        f"{accuracy.correct / (accuracy.answered or 1):.1%} overall at 100% recall, "
        f"over {accuracy.graded_call_sites:,} graded sites "
        f"({accuracy.excluded_untrustworthy:,} excluded)"
    )

    cache = CACHE / f"{corpus.name}.db"
    cache.unlink(missing_ok=True)
    with Indexer(root=corpus, cache_path=cache, measure=False) as indexer:
        indexer.index()
        report = ingest_index(indexer, index)
    sites = report.call_sites or 1
    print(
        f"  L2     {report.definition_coverage:.1%} of declarations, "
        f"{report.call_coverage:.1%} of call sites ({report.joined_call_sites:,}/{sites:,})"
    )
    print(
        f"  why    {report.calls_without_occurrence:,} "
        f"({report.calls_without_occurrence / sites:.1%}) had no SCIP occurrence at the callee; "
        f"{report.calls_without_caller:,} ({report.calls_without_caller / sites:.1%}) had one "
        f"and no enclosing entity"
    )
    print(f"  alias  {report.aliases.as_dict()}")


def main(languages: list[str]) -> int:
    failures = 0
    for language in languages:
        corpus = ROOT / "benchmarks" / "corpora" / CORPORA[language]
        print(f"\n{language} -- {CORPORA[language]}")
        if not corpus.exists():
            print("  corpus not fetched; run scripts/fetch_corpora.py")
            failures += 1
            continue
        index = CACHE / f"{CORPORA[language]}.scip"
        started = time.perf_counter()
        try:
            refused = build_index(language, corpus.resolve(), index)
        except Exception as error:  # noqa: BLE001 - report and continue to the next language
            print(f"  indexer failed: {error}")
            failures += 1
            continue
        if refused is not None:
            print(f"  {refused}")
            failures += 1
            continue
        print(f"  index  {index.stat().st_size / 1e6:.0f} MB, {time.perf_counter() - started:.0f}s")
        measure(language, corpus.resolve(), index)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))
    chosen = [name for name in sys.argv[1:] if name in CORPORA] or list(CORPORA)
    raise SystemExit(main(chosen))
