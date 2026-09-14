#!/usr/bin/env python3
"""Measure where OXN's gated ceilings sit in the distribution of real code.

P10 asks for the ceilings to stop being `judgement n=0`. The roadmap words that as fitting
them to *corpus percentiles*, and the measurement below is the argument for not doing that.

**An unweighted percentile of these distributions is not a ceiling.** Metric distributions
over real code are dominated by trivial callables: the median cognitive complexity is 0 in all
five corpora, and 92.7% of nest's 10,419 anonymous callables score 0. So "the 95th percentile"
is 2 in TypeScript and 9 in Go, and would reject most ordinary functions in either.

**LOC-weighted, the method is fine and it agrees with the ceilings we have.** Alves, Ypma &
Visser (ICSM 2010), which `docs/metrics.md` section 10.3 cites for this, weight each entity by
its own length so a 500-line function counts 500 times a one-line one. This script emits it as
`ceiling_at_weighted_percentile` as of 2026-09-14; it did not before, and the numbers lived in a
`git log` query for this file, which is how they came to be quoted for a five-corpus set after a
sixth was added. Weighted, cognitive 12 sits at P78 (go) to P98 (java) -- inside that paper's
p80/p90 high-risk band -- **except on `javascript-eslint`, where it is P48**: more than half of
that corpus's lines are in callables above the ceiling.

**They are still not fitted to it, for a reason that is not statistical.** The weighted p90
ranges 7 to 23 across these six repositories, so a fitted ceiling is a function of the corpus
set and moves whenever a corpus is re-pinned or a sixth language is added. That is the
retrieval re-pin (`freeze_retrieval_corpus.py`) with a gate behind it.

**The exceedance rate is the statistic that survives.** "What fraction of real code does this
ceiling reject" is *comparable* across corpora -- same units, same meaning -- and is the
number anyone arguing about a ceiling actually wants. It is not stable, and saying so would
be the same overclaim: `file_sloc` ranges 0% to 20.9%, and that spread is the finding rather
than noise around it.

**Two populations, because anonymous callables are not evenly distributed.** 69.4% of nest's
callables are anonymous arrow functions and 0.1% of httpx's are lambdas -- a 700x difference
in what the denominator is made of. Both rows are reported and both are needed:
`all_callables` is what the gate actually rejects, since `config.CALLABLE_KINDS` gates
lambdas too; `named` is the only row two languages can be compared on. They disagree where it
matters -- TypeScript's `function_sloc` exceedance is 3.16% over all callables and 0.67% over
named ones, so the excess is long inline callbacks, not long functions.

    python scripts/measure_ceilings.py            # re-measure and write the observations
    python scripts/measure_ceilings.py --status   # report whether it would change

`--status` exits 0 and never gates, for the same reason its retrieval siblings do not: the
corpora are pinned in `benchmarks/lock.json`, so a change here means a corpus moved or a
metric changed, and either is a thing to read rather than to re-pin past.

Each corpus must have been measured first, from inside its own root, which is where `oxn`
looks for a project::

    cd benchmarks/corpora/<name> && oxn metrics . --json --limit 1 >/dev/null
"""

from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OBSERVATIONS = ROOT / "benchmarks" / "ceiling-observations.json"
CORPORA = ROOT / "benchmarks" / "corpora"


#: The `use: threshold` corpora of `benchmarks/manifest.yaml`, as (corpus, language).
#:
#: Keyed by *corpus* rather than by language, though today there is one of each: the follow-up
#: this measurement asks for is a **second** Rust and a second Python repository, to tell
#: "Rust is written this way" apart from "ripgrep is written this way". Keying by language
#: would have made that the schema change it should not be.
#: Corpus -> the language its row is *about*. Not a label: every value below is filtered to
#: files of that language, because no real repository holds only one. nest carries 10
#: JavaScript files and eslint 36 TypeScript ones, so without the filter the TypeScript row
#: measured a little JavaScript and the JavaScript row measured rather more TypeScript --
#: the two languages that share a grammar family contaminating each other's numbers, in a
#: table whose whole purpose is to compare languages.
#: Paths inside a corpus that are not that project's code. OXN has had `exclude` in
#: `oxn.yaml` since 2026-08-30 and `oxn init` ships the globs that matter, and nobody had
#: ever pointed it at a corpus -- so the JavaScript row was about to publish 10.96%
#: cognitive exceedance with **1.1 MB of vendored JSHint** in the population: `tests/bench/
#: large.js` is JSHINT 2.4.3, `tests/performance/jshint.js` is JSHINT 2.1.8, and
#: `docs/src/assets/js/css-vars-ponyfill@2.js` is a minified third-party library. 72 of the
#: over-ceiling functions came from those three files. The other five corpora need nothing:
#: every one of their worst functions is the project's own source, checked rather than
#: assumed.
@dataclass(frozen=True, slots=True)
class Bed:
    """One threshold corpus: what to read, which language's row it is, and what to leave out.

    The three travel together everywhere -- a corpus that is not filtered to its own language
    or not stripped of vendored code is a different measurement, not a looser one -- so they
    are one value rather than three arguments passed in step.
    """

    corpus: str
    language: str
    excluded: tuple[str, ...] = ()


BEDS: tuple[Bed, ...] = (
    Bed("python-httpx", "python"),
    Bed("go-kit", "go"),
    Bed("rust-ripgrep", "rust"),
    Bed("java-spring-petclinic", "java"),
    Bed("typescript-nest", "typescript"),
    Bed("javascript-eslint", "javascript", ("docs/", "tests/bench/", "tests/performance/")),
)

#: Mirrors `config.GATED_METRICS`: rule name -> (metric key, the kinds it is gated on).
#: Duplicated as literals rather than imported so that a change to what OXN gates shows up
#: here as a diff, instead of silently re-scoping a frozen measurement.
CALLABLE = ("function", "method", "lambda")
FILE = ("module", "file")
CLASS = ("class", "interface")
GATED: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("cognitive_complexity", "cognitive_complexity", CALLABLE),
    ("cyclomatic_complexity", "cyclomatic_complexity", CALLABLE),
    ("max_nesting_depth", "max_nesting_depth", CALLABLE),
    ("parameter_count", "parameter_count", CALLABLE),
    ("function_sloc", "sloc", CALLABLE),
    ("file_sloc", "sloc", FILE),
    ("methods_per_class", "nom", CLASS),
    ("weighted_methods_per_class", "wmc", CLASS),
)

_QUANTILES = (50, 75, 90, 95, 99)

#: Mirrors `thresholds.MAX_CYCLOMATIC_COMPLEXITY`, for the redundancy question only.
MAX_CYCLOMATIC = 10


def _values(
    db: Path, metric_key: str, kinds: tuple[str, ...], bed: Bed, *, named: bool
) -> list[float]:
    """Every measured value for one metric, over the entity kinds that metric gates.

    ``named`` drops the anonymous callables. `entity.name is None` is the builder's own
    marker for them -- it is what makes it synthesise `<arrow_function@12>` -- so this asks
    the question in the same terms the graph stores rather than matching that shape back out.

    ``language`` keeps a corpus's row about the language it is named for. Filtered by asking
    `oxn.profiles` what each file is -- the same table the walk reads -- rather than by
    extension here, so the measurement and the analysis cannot disagree about what a `.mts`
    file is.
    """
    placeholders = ",".join("?" * len(kinds))
    clause = " AND e.name IS NOT NULL" if named else ""
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT m.value, e.file_path FROM metrics m JOIN entities e ON e.id = m.entity_id"
            f" WHERE m.metric_key = ? AND e.kind IN ({placeholders}){clause}",
            (metric_key, *kinds),
        ).fetchall()
    return sorted(
        value
        for value, path in rows
        if _language_of(path) == bed.language and not path.startswith(bed.excluded)
    )


def _weighted(
    db: Path, metric_key: str, kinds: tuple[str, ...], bed: Bed, *, named: bool
) -> list[tuple[float, float]]:
    """Every measured value paired with the length of the entity that carries it.

    **Alves, Ypma & Visser (ICSM 2010) weight each entity by its own lines**, so a 500-line
    function counts five hundred times a one-line one. Without that these distributions have
    median 0 -- 69.4% of nest's callables are anonymous arrows and 92.7% of those score 0 -- and
    an unweighted p95 for cognitive complexity is 2 in TypeScript against 9 in Go.

    The weight is the entity's own `sloc`, joined from the same table. `file_sloc` has none
    recorded for a file entity and needs none: there the value *is* the length, so it weights
    itself, and the paper's construction is the same either way.
    """
    placeholders = ",".join("?" * len(kinds))
    clause = " AND e.name IS NOT NULL" if named else ""
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT m.value, e.file_path, COALESCE(w.value, m.value)"
            " FROM metrics m JOIN entities e ON e.id = m.entity_id"
            " LEFT JOIN metrics w ON w.entity_id = m.entity_id AND w.metric_key = 'sloc'"
            f" WHERE m.metric_key = ? AND e.kind IN ({placeholders}){clause}",
            (metric_key, *kinds),
        ).fetchall()
    return sorted(
        (value, weight)
        for value, path, weight in rows
        if _language_of(path) == bed.language and not path.startswith(bed.excluded)
    )


def _weighted_percentile(pairs: list[tuple[float, float]], ceiling: float) -> float | None:
    """The share of *lines* living in entities at or below the ceiling.

    ``None`` when nothing carries any weight, which is not the same as zero: a corpus whose
    entities all measure 0 lines has no weighted view rather than a weighted view of nothing.
    """
    total = sum(weight for _, weight in pairs)
    if total <= 0:
        return None
    under = sum(weight for value, weight in pairs if value <= ceiling)
    return round(100.0 * under / total, 2)


def _language_of(path: str) -> str:
    """What OXN calls this file, or `""` for one it does not analyse."""
    from oxn.profiles import profile_for_path

    profile = profile_for_path(path)
    return profile.name if profile is not None else ""


def _distribution(
    values: list[float], ceiling: float, weighted: list[tuple[float, float]] | None = None
) -> dict[str, object]:
    """One corpus, one metric: the shape, and what the ceiling does to it.

    `exceedance` leads because it is the only field here that means the same thing in every
    corpus. The quantiles are reported alongside it so that the claim "the percentile is
    uninformative" can be checked rather than taken on trust.
    """
    over = len(values) - bisect.bisect_right(values, ceiling)
    return {
        "n": len(values),
        "exceedance": round(100.0 * over / len(values), 4),
        "over_ceiling": over,
        "ceiling_at_percentile": round(
            100.0 * bisect.bisect_right(values, ceiling) / len(values), 2
        ),
        # Frozen alongside the unweighted figure rather than instead of it. `docs/metrics.md`
        # quoted the weighted view for two years' worth of prose while the script emitted only
        # the unweighted one, so a reader who ran the named script found different numbers and
        # no explanation. Both are here now and each says which it is.
        "ceiling_at_weighted_percentile": (
            _weighted_percentile(weighted, ceiling) if weighted else None
        ),
        "quantiles": {
            f"p{point}": values[min(len(values) - 1, int(point / 100 * len(values)))]
            for point in _QUANTILES
        },
        "max": values[-1],
    }


def measure(ceilings: dict[str, float]) -> dict[str, object]:
    """The whole table: every gated ceiling against every threshold corpus."""
    _require_fresh_caches()
    observed: dict[str, object] = {}
    for rule, metric_key, kinds in GATED:
        per_corpus = {
            bed.corpus: _populations(
                CORPORA / bed.corpus / ".oxn/cache/graph.db",
                metric_key,
                kinds,
                ceilings[rule],
                bed,
            )
            for bed in BEDS
        }
        observed[rule] = {
            "ceiling": ceilings[rule],
            "corpora": {name: found for name, found in per_corpus.items() if found},
        }
    return observed


def _require_fresh_caches() -> None:
    """Refuse to measure a cache that is not the current builder's whole answer.

    Two ways a cache lies, and both have happened.

    **Old.** Existence was the only check, and the caches sat at schema 7 while the builder
    had moved to 9. The script measured them and reported the numbers as current. Nothing
    downstream could notice -- `tests/test_ceiling_observations.py` holds the frozen record
    against the live ceilings and never re-measures -- so a stale figure would have survived
    indefinitely under a green suite.

    **Partial.** Schema alone is not enough either, because a cache is written by whoever
    opened it, and the writers do not all want the same files. On 2026-09-10 these held 23 of
    httpx's 60, 901 of nest's 1,913 and 30 of petclinic's 50, *at the current schema*.
    Tracked down rather than guessed at: the P11 harness picks its targets from the bed's
    declared `sources` -- `httpx`, not the repository -- so it indexes a subtree **by
    design**, into the same `.oxn/cache/graph.db` this script reads. Neither tool is wrong;
    the assumption that a corpus cache is a whole-tree index is. Freezing one of those would
    have put a distribution over 4,000 callables into `calibration.py` as one over 10,000.

    So the count is checked against the walk, and against the same walk the indexer uses:
    `Indexer.sources` exists precisely so a caller cannot measure one file set and report
    another. A measurement that cannot say whether it measured *all* of today's code is not a
    measurement either.
    """
    from oxn.graph.store import SCHEMA_VERSION

    stale: list[str] = []
    for bed in BEDS:
        name = bed.corpus
        db = CORPORA / name / ".oxn/cache/graph.db"
        if not db.exists():
            stale.append(f"{name}: no cache")
            continue
        found = _schema_version(db)
        if found != str(SCHEMA_VERSION):
            stale.append(f"{name}: schema {found}, builder writes {SCHEMA_VERSION}")
            continue
        cached, walked = _coverage(CORPORA / name, db)
        if cached != walked:
            stale.append(f"{name}: {cached} of {walked} files cached")
            continue
        measured, entities = _measured(db)
        if measured != entities:
            stale.append(f"{name}: {measured} of {entities} entities measured")
    if stale:
        raise SystemExit(
            "refusing to measure stale caches:\n  "
            + "\n  ".join(stale)
            + "\n\nRun `oxn metrics . --json --limit 1` from inside each corpus root first"
            " (see this file's docstring); a schema bump makes every cache stale, and so does"
            " anything that indexed part of the tree."
        )


def _measured(db: Path) -> tuple[int, int]:
    """(entities carrying any metric, entities in the cache) for one corpus.

    **The third form of the same failure, and the one the file count could not see.** A cache
    can hold every file of today's tree and still have lost the *metrics* for most of them:
    `GraphStore.put_file` is a replace and takes that file's metric rows with it, so anything
    that re-indexes without measuring leaves the entities behind and the numbers gone. Found on
    2026-09-14 with `python-httpx` at **557 of 1,302 entities measured** and 60 of 60 files
    cached -- the guard above passed it, and a fresh measurement put cognitive complexity's
    population at 447 callables where the frozen record says 1,135. Freezing that would have
    republished a distribution over a third of the corpus as one over all of it.

    Neither count alone is the measurement's precondition. "Every file of the tree is here" and
    "every entity here was measured" are different claims, and this script needs both.
    """
    with sqlite3.connect(db) as connection:
        entities = int(connection.execute("SELECT count(*) FROM entities").fetchone()[0])
        measured = int(
            connection.execute("SELECT count(DISTINCT entity_id) FROM metrics").fetchone()[0]
        )
    return measured, entities


def _coverage(root: Path, db: Path) -> tuple[int, int]:
    """(files in the cache, files the indexer would look at) for one corpus."""
    from oxn.graph.indexer import Indexer

    with sqlite3.connect(db) as connection:
        cached = int(connection.execute("SELECT count(*) FROM files").fetchone()[0])
    with Indexer(root=root, cache_path=db) as indexer:
        return cached, len(indexer.sources())


def _schema_version(db: Path) -> str:
    """The schema the cache was written at, or a marker when it does not say."""
    with sqlite3.connect(db) as connection:
        try:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.DatabaseError:
            return "unreadable"
    return str(row[0]) if row else "unrecorded"


def _populations(
    db: Path, metric_key: str, kinds: tuple[str, ...], ceiling: float, bed: Bed
) -> dict[str, object]:
    """One corpus, one metric, both populations -- and empty when it measures neither.

    `file_sloc` has no `named` row to drop and no anonymous one to keep, so the two come out
    identical there; that is the honest answer rather than a special case.
    """
    measured = {
        "all_callables": (
            _values(db, metric_key, kinds, bed, named=False),
            _weighted(db, metric_key, kinds, bed, named=False),
        ),
        "named": (
            _values(db, metric_key, kinds, bed, named=True),
            _weighted(db, metric_key, kinds, bed, named=True),
        ),
    }
    return {
        name: _distribution(values, ceiling, weighted)
        for name, (values, weighted) in measured.items()
        if values
    }


def audit_class_ceilings(ceilings: dict[str, float]) -> dict[str, object]:
    """What the class ceilings actually reject, and whether either is redundant.

    Both are positioned by a single control (`tests/test_class_scope_evasion.py`), which is
    the circularity `TRIVIAL_HELPER`'s own `fit_when` warns about: a value fitted to one
    fixture by the person who chose it. This is the non-circular half -- not another authored
    fixture but a census of what the ceilings reject in code nobody wrote for them.

    The load-bearing number is `wmc_without_a_hot_method`: classes over the WMC ceiling that
    contain no method over the *per-function* cyclomatic ceiling. Those are the ones no other
    gate can see -- every method individually fine, the accumulation the whole problem, which
    is the God Class shape by definition.
    """
    nom_limit, wmc_limit = ceilings["methods_per_class"], ceilings["weighted_methods_per_class"]
    tally = {
        "rejected": 0,
        "both": 0,
        "nom_only": 0,
        "wmc_only": 0,
        "wmc_without_a_hot_method": 0,
        "in_test_files": 0,
        "interfaces": 0,
    }
    for bed in BEDS:
        _audit_corpus(CORPORA / bed.corpus / ".oxn/cache/graph.db", nom_limit, wmc_limit, tally)
    return tally


def _audit_corpus(db: Path, nom_limit: float, wmc_limit: float, tally: dict[str, int]) -> None:
    """One corpus's contribution to the census above."""
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT e.kind, e.file_path, m.value,"
            " (SELECT value FROM metrics w WHERE w.entity_id = e.id AND w.metric_key = 'wmc'),"
            " (SELECT MAX(c.value) FROM entities k JOIN metrics c ON c.entity_id = k.id"
            "  WHERE k.parent_id = e.id AND c.metric_key = 'cyclomatic_complexity')"
            " FROM metrics m JOIN entities e ON e.id = m.entity_id WHERE m.metric_key = 'nom'"
        ).fetchall()
    for row in rows:
        for label in _verdicts(row, nom_limit, wmc_limit):
            tally[label] += 1


#: (over the NOM ceiling, over the WMC ceiling) -> which tally the class belongs in. A table
#: rather than a branch chain: cyclomatic complexity charges for every boolean operator
#: whether or not it nests, and the question here is a lookup, not a policy.
_WHICH = {(True, True): "both", (True, False): "nom_only", (False, True): "wmc_only"}


def _verdicts(row: tuple, nom_limit: float, wmc_limit: float) -> tuple[str, ...]:
    """Every tally one class contributes to; empty when both ceilings accept it."""
    kind, path, nom, wmc, worst = row
    over_wmc = (wmc or 0) > wmc_limit
    which = _WHICH.get((nom > nom_limit, over_wmc))
    if which is None:
        return ()
    labels = {
        "rejected": True,
        which: True,
        "wmc_without_a_hot_method": over_wmc and (worst or 0) <= MAX_CYCLOMATIC,
        "interfaces": kind == "interface",
        "in_test_files": "test" in path.lower() or "spec" in path.lower(),
    }
    return tuple(label for label, hit in labels.items() if hit)


def _payload() -> dict[str, object]:
    from oxn import thresholds
    from oxn.config import GATED_METRICS

    ceilings = {
        rule: float(getattr(thresholds, gate.threshold))
        for rule, gate in GATED_METRICS.items()
        if rule in {name for name, _, _ in GATED}
    }
    return {
        "class_ceiling_audit": audit_class_ceilings(ceilings),
        "note": (
            "Exceedance is the headline: the fraction of real code each ceiling rejects. "
            "Percentiles are recorded but are NOT a fitting target -- these distributions "
            "have median 0, so a p95 ceiling would be 2 in TypeScript and 9 in Go. Read "
            "`named` to compare languages and `all_callables` to see what the gate rejects."
        ),
        "corpora": {bed.corpus: bed.language for bed in BEDS},
        "observed": measure(ceilings),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="Report drift; never gates.")
    args = parser.parse_args()

    fresh = _payload()
    if not args.status:
        OBSERVATIONS.write_text(json.dumps(fresh, indent=2, sort_keys=True) + "\n")
        print(f"wrote {OBSERVATIONS.relative_to(ROOT)}")
        return 0

    if not OBSERVATIONS.exists():
        print("no frozen observations yet; run without --status")
        return 0
    stale = json.loads(OBSERVATIONS.read_text()) != fresh
    print("observations differ from a fresh measurement" if stale else "observations are current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
