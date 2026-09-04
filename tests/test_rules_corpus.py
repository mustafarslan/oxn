"""P7's exit criterion, measured on real repositories rather than on a fixture.

`test_rules_parity.py` proves the rule engine reproduces the hand-coded gate on a tree
built to exercise every contract kind. That fixture is twelve files. These run the same
comparison over the pinned corpora, which is where the interesting disagreements would be:
1,913 real TypeScript files and 60 real Python ones, in five languages' worth of grammar
edge cases nobody designed a fixture around.

They also assert the evaluator's *shape*. The first version of the join scanned every tuple
of a relation for every binding, which is quadratic and was invisible on small inputs: 72
seconds for 60 files against a 200 ms hook budget, and a 1,913-file corpus that ran for
thirteen minutes without finishing. A fixture would never have shown that. The budgets
below are deliberately loose -- they catch a regression in kind, not a slow afternoon.

    python scripts/fetch_corpora.py --name python-httpx --name typescript-nest
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import pytest

from oxn.check import CheckReport, _architecture, _measure
from oxn.config import Config
from oxn.graph.contracts import assign_layers
from oxn.graph.indexer import Indexer
from oxn.graph.sources import iter_source_files
from oxn.rules.builtin import all_rules
from oxn.rules.engine import evaluate
from oxn.rules.facts import file_facts, graph_facts

CORPORA = Path("benchmarks/corpora")
ROOTS = {
    "python-httpx": CORPORA / "python-httpx",
    "typescript-nest": CORPORA / "typescript-nest",
}

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not all(root.exists() for root in ROOTS.values()),
        reason="corpora not fetched; see scripts/fetch_corpora.py",
    ),
]

#: Generous. Measured on the development machine: 0.13 s for httpx, 3.4 s for nest. The
#: point is to fail if the joins stop being indexed, which costs three orders of magnitude.
EVALUATION_BUDGET_S = 60.0

#: Below this a parity result proves nothing -- two empty lists agree perfectly.
MIN_FINDINGS = 50


def _identity(finding) -> tuple:
    return (
        finding.rule,
        finding.path,
        finding.entity,
        finding.line,
        round(finding.value, 6),
        round(finding.ceiling, 6),
        finding.blocking,
    )


@pytest.fixture(scope="module")
def cache(tmp_path_factory) -> Path:
    """A throwaway index, because these tests are the ones that index other people's code.

    Running this lane used to leave `.oxn/cache/graph.db` at 176 MB, 2,389 of its 2,519
    files belonging to httpx and nest -- third-party trees indexed into the cache OXN gates
    itself with, by the one module that deliberately drops the exclusion keeping them out.
    That is the mistake the `exclude` fix was about, arriving from the test side.
    """
    return tmp_path_factory.mktemp("rules-corpus") / "graph.db"


@pytest.fixture(scope="module", autouse=True)
def _never_the_project_cache(cache):
    """Redirect every indexer in this module, including the ones inside `oxn.check`.

    `check.py` derives its cache from `settings.root`; an absolute default overrides that
    join, which is the least invasive way to keep those writes out of the project cache
    without giving `Config` a field that exists only for a test. Autouse and module-scoped
    so a test added later cannot forget -- `test_the_hook_path_reports_no_adr_scope_findings`
    was written after the fixture below and wrote two corpus files into the real cache on
    every run.
    """
    patch = pytest.MonkeyPatch()
    patch.setattr("oxn.graph.store.DEFAULT_CACHE_PATH", cache)
    yield
    patch.undo()


@pytest.fixture(scope="module", params=sorted(ROOTS), ids=sorted(ROOTS))
def corpus(request, cache):
    """Both implementations run over one corpus, with the evaluation timed."""
    root = ROOTS[request.param]
    # OXN's own `oxn.yaml` excludes `benchmarks/corpora/*` -- the corpora are fetched
    # third-party trees and have no business in OXN's own gate or metrics. These tests are
    # the one caller that *wants* them, so the exclusion is dropped here explicitly. Without
    # this the corpus is empty and parity holds vacuously; `MIN_FINDINGS` below is what
    # turns that from a silent pass into a failure.
    settings = replace(Config.load(), exclude=())
    targets = [root]

    report = CheckReport()
    hand_coded = _measure(targets, settings, report) + _architecture(targets, settings, report)

    with Indexer(cache_path=cache) as indexer:
        indexer.index(targets)
        sources = indexer.sources(targets)
        wanted = [indexer.relative(path) for path in sources]
        layers = assign_layers(wanted, settings.layers) if settings.layers else {}
        facts = file_facts(indexer.store, wanted, settings, layers)
        if settings.contracts:
            from oxn.graph.depgraph import build_dependency_graph

            graph_facts(facts, build_dependency_graph(indexer.root, sources), settings)
        start = time.perf_counter()
        from_rules = evaluate(all_rules(settings), facts)
        seconds = time.perf_counter() - start

    return {
        "name": request.param,
        "files": len(wanted),
        "facts": sum(len(rows) for rows in facts.relations.values()),
        "hand_coded": hand_coded,
        "from_rules": from_rules,
        "seconds": seconds,
    }


def test_the_corpus_produces_enough_findings_to_compare(corpus) -> None:
    """Guarding the guard: parity over two empty lists is not evidence of anything.

    This exact trap fired during development -- the first parity run was against `src`,
    which by then had zero violations, and reported IDENTICAL while comparing nothing.
    """
    assert len(corpus["hand_coded"]) >= MIN_FINDINGS, (
        f"{corpus['name']} produced only {len(corpus['hand_coded'])} findings; "
        f"parity here would be vacuous"
    )


def test_the_rules_reproduce_the_hand_coded_findings(corpus) -> None:
    old = sorted(map(_identity, corpus["hand_coded"]))
    new = sorted(map(_identity, corpus["from_rules"]))
    assert old == new


def test_finding_identity_is_unchanged(corpus) -> None:
    """`rule|path|entity` is what the baseline is keyed on, across a real corpus."""
    old = {(f.rule, f.path, f.entity) for f in corpus["hand_coded"]}
    new = {(f.rule, f.path, f.entity) for f in corpus["from_rules"]}
    assert old == new


def test_evaluation_stays_within_budget(corpus) -> None:
    """A regression detector for the join, not a benchmark.

    Unindexed, this took 72 s on the 60-file corpus and never finished on the 1,913-file
    one. Any number under a minute means the joins are still probing rather than scanning.
    """
    assert corpus["seconds"] < EVALUATION_BUDGET_S, (
        f"{corpus['name']}: {corpus['files']} files, {corpus['facts']} facts, "
        f"evaluated in {corpus['seconds']:.1f} s -- the joins are probably scanning again"
    )


def test_the_hook_path_reports_no_adr_scope_findings(corpus) -> None:
    """A per-file check must never claim an ADR governs nothing.

    "This scope matches nothing" is a claim about the repository, and the hook checks one
    file. Before this was gated on `--deep`, checking `src/oxn/check.py` alone reported two
    of OXN's own ADRs as dead -- which on a `PostToolUse` hook is noise on every save.
    """
    from oxn.check import run_check

    settings = replace(Config.load(), exclude=())
    one_file = next(iter(sorted(iter_source_files([ROOTS[corpus["name"]]]))))
    report = run_check([str(one_file)], config=settings, deep=False, use_baseline=False)
    assert not [f for f in report.findings + report.advisory if f.rule.startswith("adr:")]


def test_the_corpus_never_reaches_the_project_cache(corpus) -> None:
    """The end state, asserted rather than assumed, since the redirection is a fixture and
    fixtures are easy to bypass by writing a test that builds its own indexer.

    Matched on the path *prefix*: a substring search for "corpora" also finds
    `scripts/fetch_corpora.py`, which is how this check first reported a leak that was not
    one.
    """
    import sqlite3

    project = Path(".oxn/cache/graph.db")
    if not project.exists():
        return
    with sqlite3.connect(project) as connection:
        leaked = connection.execute(
            "select count(*) from files where path like 'benchmarks/corpora/%'"
        ).fetchone()[0]
    assert leaked == 0, (
        f"{leaked} corpus files are in OXN's own cache. If this is left over from before "
        "the redirection, `rm -rf .oxn/cache` and re-run; it rebuilds."
    )
