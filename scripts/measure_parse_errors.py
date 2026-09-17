"""How often a tree-sitter grammar rejects code that is actually fine.

    python scripts/measure_parse_errors.py                 # every fetched corpus
    python scripts/measure_parse_errors.py --show 20       # more example paths

**This is the measurement that decides whether an unparseable file may fail the gate.**
`oxn check` counted a file it could not parse as checked and clean -- 0 violations, exit 0 --
so an agent that wrote broken syntax got a green light from the tool whose whole job is
catching that. The obvious fix is to make it blocking, and the obvious fix is only safe if
the grammars do not reject *valid* code, because a gate with false positives is worse than
the hole it closes.

The distinction this script exists to draw is that a `has_error` hit has two causes and only
one of them is a false positive:

* **The file really is invalid.** `javascript-eslint` ships hundreds of deliberately broken
  sources under `tests/fixtures/`, because a linter's test suite is largely malformed input.
  A gate firing on those is *correct*.
* **The grammar is behind the language.** New syntax -- Python 3.12 type parameters, Rust
  let-else, Java 21 patterns -- parses as an error in an older grammar. This is the one that
  would block a user writing perfectly good code, and it is the number that decides.

So hits are split by whether the path looks like test-fixture material, the counts are
reported per language, and the sample paths are printed for the second group so they can be
opened and judged by hand rather than trusted.

**Measured 2026-09-17, after the `.tsx` fix this script found.** 5 non-fixture failures in
5,580 files -- **0.090%** -- and every one of them was opened:

=======================  =======  =======  ======  ========
corpus                    parsed  fixture   other   other %
=======================  =======  =======  ======  ========
`adr-agentic-dev-team`     1,229        0       0    0.000%
`adr-elsa-core`               14        0       0    0.000%
`adr-tessellation`             8        0       3   37.500%
`go-kit`                     256        0       0    0.000%
`java-spring-petclinic`       50        0       0    0.000%
`javascript-eslint`        1,487        8       0    0.000%
`python-httpx`                60        0       0    0.000%
`rust-ripgrep`               110        0       0    0.000%
`slop-code-bench`            329        0       0    0.000%
`typescript-nest`          1,913        0       2    0.105%
=======================  =======  =======  ======  ========

**The first run found a bug rather than a rate, which is why the fix precedes the table.**
`.tsx` was being parsed with the `typescript` grammar, which cannot parse JSX -- `.tsx` needs
the separate `tsx` grammar the pack also ships. **10 of the 11 `.tsx` files in these corpora
failed to parse; all 10 parse now.** Eight of them are the `adr-agentic-dev-team` column's
former fixture hits, which were never malformed at all: they are valid React components used
as eval *inputs* that happen to live under `evals/fixtures/`.

The five that remain are two causes, and only one is a false positive:

* **`typescript-nest`, 2 files** -- `@((...)())`, a parenthesised decorator expression, in
  `packages/microservices/test/decorators/`. Valid TypeScript that the pack's grammar version
  predates. **This is a genuine false positive**, and at 2 of 1,913 it is the honest caveat on
  making an unparseable file fail the gate.
* **`adr-tessellation`, 3 files** -- `swagger-ui*.js`, vendored minified bundles whose first
  line is 184,623 characters. Valid JavaScript, but build artifacts rather than source; the
  open question they raise is whether a default `exclude` should cover vendored bundles, not
  whether the grammar is right.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPORA = ROOT / "benchmarks" / "corpora"

#: Path fragments that mark a tree's own deliberately-broken input. Matched against the
#: relative path, not guessed from content: a file under `tests/fixtures` that fails to parse
#: is doing its job, and counting it as a grammar gap would argue the gate into being
#: advisory for the wrong reason.
FIXTURE_MARKERS = (
    "tests/fixtures/",
    "test/fixtures/",
    "testdata/",
    "fixtures/",
    "__fixtures__/",
    "test-data/",
)


@dataclass(slots=True)
class Row:
    """One corpus: how much parsed, and which failures are the ones that matter."""

    name: str
    parsed: int = 0
    fixtures: list[str] = field(default_factory=list)
    others: list[str] = field(default_factory=list)

    @property
    def share(self) -> float:
        """Non-fixture failures as a fraction of files parsed -- the deciding number."""
        return len(self.others) / self.parsed if self.parsed else 0.0


def _is_fixture(relative: str) -> bool:
    return any(marker in relative for marker in FIXTURE_MARKERS)


def sweep(corpus: Path) -> Row:
    """Parse every source in one corpus and split its failures by cause."""
    from oxn.config import Config
    from oxn.graph.indexer import Indexer

    settings = Config.load(corpus)
    with tempfile.TemporaryDirectory() as scratch:
        cache = Path(scratch) / "graph.db"
        with Indexer(root=corpus, exclude=settings.exclude, cache_path=cache) as indexer:
            report = indexer.index([corpus])
    return Row(
        name=corpus.name,
        parsed=report.parsed,
        fixtures=[path for path in report.incomplete if _is_fixture(path)],
        others=[path for path in report.incomplete if not _is_fixture(path)],
    )


def render(rows: list[Row], show: int) -> None:
    """The table, then the paths behind the column that decides.

    The suspect paths are printed rather than summarised because the decision rests on what
    they *are*: a count cannot tell a grammar gap from a corpus that vendors a snippet of
    another language under a misleading extension.
    """
    print(f"{'corpus':28s} {'parsed':>8s} {'fixture':>8s} {'other':>8s} {'other %':>9s}")
    print("-" * 66)
    for row in rows:
        print(
            f"{row.name:28s} {row.parsed:8d} {len(row.fixtures):8d} "
            f"{len(row.others):8d} {row.share:8.3%}"
        )

    parsed = sum(row.parsed for row in rows)
    others = sum(len(row.others) for row in rows)
    print("-" * 66)
    print(f"{'total':28s} {parsed:8d} {'':>8s} {others:8d} {others / parsed if parsed else 0:8.3%}")

    for row in rows:
        if row.others:
            print(f"\n{row.name}: {len(row.others)} non-fixture hit(s), first {show}")
            print("\n".join(f"  {path}" for path in sorted(row.others)[:show]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpora", nargs="*", help="Corpus directories; default is all fetched.")
    parser.add_argument("--show", type=int, default=8, help="Example paths to print per corpus.")
    args = parser.parse_args(argv)

    targets = [Path(name) for name in args.corpora] or sorted(
        path for path in CORPORA.iterdir() if path.is_dir()
    )
    if not targets:
        print("no corpora fetched; run scripts/fetch_corpora.py", file=sys.stderr)
        return 1

    render([sweep(corpus) for corpus in targets], args.show)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
