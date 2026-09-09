#!/usr/bin/env python3
"""Measure where OXN's gated ceilings sit in the distribution of real code.

P10 asks for the ceilings to stop being `judgement n=0`. The roadmap words that as fitting
them to *corpus percentiles*, and the measurement below is the argument for not doing that.

**An unweighted percentile of these distributions is not a ceiling.** Metric distributions
over real code are dominated by trivial callables: the median cognitive complexity is 0 in all
five corpora, and 92.8% of nest's 10,396 anonymous callables score 0. So "the 95th percentile"
is 2 in TypeScript and 9 in Go, and would reject most ordinary functions in either.

**LOC-weighted, the method is fine and it agrees with the ceilings we have.** Alves, Ypma &
Visser (ICSM 2010), which `docs/metrics.md` section 10.3 cites for this, weight each entity by
its own length so a 500-line function counts 500 times a one-line one. Weighted, cognitive 12
sits at P79 (go) to P98 (java) -- inside that paper's p80/p90 high-risk band. This script does
not emit the weighted view; `git log` for this file has the query, and ROADMAP's P10 amendment
has the numbers. It is a corroboration, not the thing being frozen.

**They are still not fitted to it, for a reason that is not statistical.** The weighted p90
ranges 7 to 23 across these five repositories, so a fitted ceiling is a function of the corpus
set and moves whenever a corpus is re-pinned or a sixth language is added. That is the
retrieval re-pin (`freeze_retrieval_corpus.py`) with a gate behind it.

**The exceedance rate is the statistic that survives.** "What fraction of real code does this
ceiling reject" is *comparable* across corpora -- same units, same meaning -- and is the
number anyone arguing about a ceiling actually wants. It is not stable, and saying so would
be the same overclaim: `file_sloc` ranges 0% to 20.9%, and that spread is the finding rather
than noise around it.

**Two populations, because anonymous callables are not evenly distributed.** 69.3% of nest's
callables are anonymous arrow functions and 0.1% of httpx's are lambdas -- a 700x difference
in what the denominator is made of. Both rows are reported and both are needed:
`all_callables` is what the gate actually rejects, since `config.CALLABLE_KINDS` gates
lambdas too; `named` is the only row two languages can be compared on. They disagree where it
matters -- TypeScript's `function_sloc` exceedance is 3.17% over all callables and 0.71% over
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
BEDS: tuple[tuple[str, str], ...] = (
    ("python-httpx", "python"),
    ("go-kit", "go"),
    ("rust-ripgrep", "rust"),
    ("java-spring-petclinic", "java"),
    ("typescript-nest", "typescript"),
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


def _values(db: Path, metric_key: str, kinds: tuple[str, ...], *, named: bool) -> list[float]:
    """Every measured value for one metric, over the entity kinds that metric gates.

    ``named`` drops the anonymous callables. `entity.name is None` is the builder's own
    marker for them -- it is what makes it synthesise `<arrow_function@12>` -- so this asks
    the question in the same terms the graph stores rather than matching that shape back out.
    """
    placeholders = ",".join("?" * len(kinds))
    clause = " AND e.name IS NOT NULL" if named else ""
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT m.value FROM metrics m JOIN entities e ON e.id = m.entity_id"
            f" WHERE m.metric_key = ? AND e.kind IN ({placeholders}){clause}",
            (metric_key, *kinds),
        ).fetchall()
    return sorted(value for (value,) in rows)


def _distribution(values: list[float], ceiling: float) -> dict[str, object]:
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
        "quantiles": {
            f"p{point}": values[min(len(values) - 1, int(point / 100 * len(values)))]
            for point in _QUANTILES
        },
        "max": values[-1],
    }


def measure(ceilings: dict[str, float]) -> dict[str, object]:
    """The whole table: every gated ceiling against every threshold corpus."""
    missing = [name for name, _ in BEDS if not (CORPORA / name / ".oxn/cache/graph.db").exists()]
    if missing:
        raise SystemExit(
            f"no measured cache for {', '.join(missing)}. Run `oxn metrics . --json --limit 1`"
            " from inside each corpus root first (see this file's docstring)."
        )
    observed: dict[str, object] = {}
    for rule, metric_key, kinds in GATED:
        per_corpus = {
            corpus: _populations(
                CORPORA / corpus / ".oxn/cache/graph.db", metric_key, kinds, ceilings[rule]
            )
            for corpus, _ in BEDS
        }
        observed[rule] = {
            "ceiling": ceilings[rule],
            "corpora": {name: found for name, found in per_corpus.items() if found},
        }
    return observed


def _populations(
    db: Path, metric_key: str, kinds: tuple[str, ...], ceiling: float
) -> dict[str, object]:
    """One corpus, one metric, both populations -- and empty when it measures neither.

    `file_sloc` has no `named` row to drop and no anonymous one to keep, so the two come out
    identical there; that is the honest answer rather than a special case.
    """
    measured = {
        "all_callables": _values(db, metric_key, kinds, named=False),
        "named": _values(db, metric_key, kinds, named=True),
    }
    return {name: _distribution(values, ceiling) for name, values in measured.items() if values}


def _payload() -> dict[str, object]:
    from oxn import thresholds
    from oxn.config import GATED_METRICS

    ceilings = {
        rule: float(getattr(thresholds, gate.threshold))
        for rule, gate in GATED_METRICS.items()
        if rule in {name for name, _, _ in GATED}
    }
    return {
        "note": (
            "Exceedance is the headline: the fraction of real code each ceiling rejects. "
            "Percentiles are recorded but are NOT a fitting target -- these distributions "
            "have median 0, so a p95 ceiling would be 2 in TypeScript and 9 in Go. Read "
            "`named` to compare languages and `all_callables` to see what the gate rejects."
        ),
        "corpora": dict(BEDS),
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
