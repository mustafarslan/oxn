"""The JavaScript import graph against dependency-cruiser.

P4's exit criterion asks for graph-equality against an independent tool per language: grimp
for Python, "dependency-cruiser and jdeps follow". This is that comparison for JavaScript,
and the pattern earns its place the same way grimp's did -- on the first run it found a real
defect, imports inside a `.d.ts` recorded as runtime edges.

**TypeScript is deliberately not compared here, and the reason has a version number.**
`docs/divergences.md` names the rule that matters most in TypeScript resolution: source
imports the *emitted* name, so `./x.js` means `./x.ts`. dependency-cruiser resolves that only
with `enhancedResolveOptions.extensionAlias`, which its 18.3.1 configuration schema does not
accept -- every relative import in `typescript-nest` comes back `couldNotResolve`. An oracle
that cannot resolve the corpus is not an oracle, so TypeScript stays characterised rather
than compared, and that is recorded rather than worked around with a second tool.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.oracle

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "benchmarks" / "corpora" / "javascript-eslint"

requires_depcruise = pytest.mark.skipif(
    shutil.which("depcruise") is None,
    reason="dependency-cruiser not installed; npm install -g dependency-cruiser typescript",
)

#: Written per-run rather than committed. The only settings that matter are not following
#: `node_modules` -- neither corpus has one -- and that this is the default resolver, so the
#: comparison is against the tool as it ships.
CONFIG = """module.exports = { options: { doNotFollow: { path: "node_modules" } } };"""


def _their_edges(directories: list[str], config: Path, parsed: set[str]) -> set[tuple[str, str]]:
    """dependency-cruiser's in-tree edges, restricted to the files OXN parses.

    ``parsed`` is the filter, not an afterthought: it is what drops Node's core modules and
    the JSON manifests -- `require("../package.json")` is a real import and not a dependency
    between source files -- without needing a rule for either.
    """
    finished = subprocess.run(  # noqa: S603 - fixed argv, corpus path from this file
        ["depcruise", "--config", str(config), "--output-type", "json", *directories],
        cwd=CORPUS,
        capture_output=True,
        text=True,
        check=False,
    )
    if not finished.stdout:
        pytest.fail(f"dependency-cruiser produced nothing: {finished.stderr.strip()[:400]}")
    payload = json.loads(finished.stdout)
    return {
        (module["source"], dependency["resolved"])
        for module in payload["modules"]
        for dependency in module["dependencies"]
        if not dependency["couldNotResolve"]
        and module["source"] in parsed
        and dependency["resolved"] in parsed
    }


def _our_edges(cache: Path) -> tuple[set[tuple[str, str]], set[str], list[Path]]:
    """OXN's in-tree import edges, the files it parsed, and the paths behind them.

    The file set comes back with the edges because the two must not be derived separately:
    every wrong answer this comparison produced came from measuring one tool over a set the
    other never saw.
    """
    from oxn.graph.depgraph import build_dependency_graph
    from oxn.graph.indexer import Indexer

    with Indexer(root=CORPUS, cache_path=cache) as indexer:
        files = list(indexer.sources([CORPUS]))
        parsed = {indexer.relative(path) for path in files}
        graph = build_dependency_graph(CORPUS, files)
    edges = {
        (source, target)
        for source, targets in graph.files.items()
        for target in targets
        if source in parsed and target in parsed
    }
    return edges, parsed, files


@requires_depcruise
@pytest.mark.skipif(not CORPUS.exists(), reason="corpora not fetched")
def test_every_javascript_import_divergence_is_explained(tmp_path: Path) -> None:
    """Both tools resolve Node's own rules; the divergences must reduce to named causes.

    Measured 2026-09-17 on `javascript-eslint`, the whole corpus: **1,480 edges agreed of the
    1,489 OXN records and the 1,486 dependency-cruiser does, over 1,487 files -- 98.997%.**
    The fifteen that differ reduce to three rules, named under `_EXPLAINED`.

    Both tools must be given the same files or the comparison is meaningless, and getting that
    wrong is the failure this test kept reproducing: restricted to `lib/rules`, every import
    reaching `../shared` looked like an edge OXN had missed; restricted to `lib`, every import
    of `conf/` did. The set comes from `oxn.yaml` now, through `Indexer.sources`, and both
    sides are filtered to it.
    """
    config = tmp_path / "depcruise.cjs"
    config.write_text(CONFIG)
    ours, parsed, files = _our_edges(tmp_path / "graph.db")

    # The same set, handed to both: every top-level entry OXN's own config admits, files at
    # the root included. Passing a subtree instead makes every import that leaves it look like
    # a missing edge -- which it is not, and which this comparison reported three times before
    # the two file sets were tied together, the last time because root-level `Makefile.js` and
    # `eslint.config.js` were dropped by a rule that only collected directories.
    directories = sorted({path.split("/")[0] for path in parsed})
    theirs = _their_edges(directories, config, parsed)
    assert theirs, "dependency-cruiser resolved nothing; the corpus or the tool is wrong"

    diverging = ours ^ theirs
    unexplained = diverging - _EXPLAINED
    assert not unexplained, (
        f"{len(unexplained)} import edge(s) neither tool explains: {sorted(unexplained)[:10]}"
    )
    # An explanation that no longer describes anything is the other way this rots: the edge
    # got fixed, or the corpus moved, and the list quietly stopped being checked.
    stale = _EXPLAINED - diverging
    assert not stale, f"{len(stale)} explanation(s) describe no divergence: {sorted(stale)}"

    # A floor under the measured 98.997%, not a target. The real guard is the two assertions
    # above -- every divergence named, every name still describing one -- and this catches the
    # wholesale case they cannot: a resolver change that makes both sets small and agreeing.
    agreement = len(ours & theirs) / len(ours | theirs)
    assert agreement >= 0.98, f"{agreement:.2%} agreement on {len(files)} files"


#: Divergences with a named cause, measured 2026-09-17. **Three rules, and each tool is more
#: capable than the other on one of them**, which is the useful thing this comparison found.
#:
#: 1. **Node's `main`, and workspace packages, resolved by dependency-cruiser and not by OXN.**
#:    `require("..")` reaches `lib/api.js` through the package's own `package.json` `main`, and
#:    `require("@eslint/js")` reaches `packages/js/src/index.js` the same way. OXN declines
#:    both by the rule `docs/divergences.md` already states for `exports` maps: a wrong edge
#:    corrupts every downstream metric silently, where a missing one is merely absent.
#: 2. **A workspace package name resolved by OXN and not by dependency-cruiser.**
#:    `require("eslint-config-eslint/cjs")` is an in-tree file, and dependency-cruiser needs
#:    the `node_modules` symlink npm would have created; neither corpus has one installed.
#: 3. **TypeScript's emitted-name imports, resolved by OXN and not by dependency-cruiser.**
#:    `./helper.js` means `./helper.ts`, which needs `enhancedResolveOptions.extensionAlias` --
#:    absent from 18.3.1's configuration schema. It is why `typescript-nest` is not compared
#:    here at all, and these `.ts`/`.mts` sources are the same limitation inside eslint.
#:
#: The set may only shrink by fixing something, and the test fails on an entry that no longer
#: diverges as loudly as on one that newly does.
_EXPLAINED: frozenset[tuple[str, str]] = frozenset(
    {
        ("Makefile.js", "packages/js/src/index.js"),
        ("eslint.config.js", "packages/eslint-config-eslint/cjs.js"),
        (
            "packages/eslint-config-eslint/tests/types/types.test.mts",
            "packages/eslint-config-eslint/base.js",
        ),
        (
            "packages/eslint-config-eslint/tests/types/types.test.mts",
            "packages/eslint-config-eslint/cjs.js",
        ),
        (
            "packages/eslint-config-eslint/tests/types/types.test.mts",
            "packages/eslint-config-eslint/formatting.js",
        ),
        (
            "packages/eslint-config-eslint/tests/types/types.test.mts",
            "packages/eslint-config-eslint/index.js",
        ),
        ("tests/conf/eslint-all.js", "packages/js/src/index.js"),
        ("tests/conf/eslint-recommended.js", "packages/js/src/index.js"),
        (
            "tests/fixtures/ts-config-files/ts/const-enums/eslint.config.ts",
            "tests/fixtures/ts-config-files/helper.ts",
        ),
        (
            "tests/fixtures/ts-config-files/ts/exports-promise/eslint.config.ts",
            "tests/fixtures/ts-config-files/ts/exports-promise/some-external-config.mts",
        ),
        (
            "tests/fixtures/ts-config-files/ts/exports-promise/some-external-config.mts",
            "tests/fixtures/ts-config-files/helper.ts",
        ),
        (
            "tests/fixtures/ts-config-files/ts/local-namespace/eslint.config.ts",
            "tests/fixtures/ts-config-files/helper.ts",
        ),
        ("tests/tools/eslint-fuzzer.js", "lib/api.js"),
        ("tools/fuzzer-runner.js", "lib/api.js"),
        ("tools/generate-formatter-examples.js", "packages/js/src/index.js"),
    }
)
