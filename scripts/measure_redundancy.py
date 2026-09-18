"""Which gated rule catches something no other gated rule catches?

`MAX_NESTING_DEPTH`'s own `fit_when` asked for exactly this -- "evidence it catches anything
`cognitive_complexity` does not catch first" -- and nobody had taken it. A rule with no
unique catches spends a constraint from a budget (`MAX_BUNDLE_CONSTRAINTS`, itself a
judgement) to repeat a finding another rule already made.

**What this counts, and what it does not.** The population is entities over the ceiling in
six committed repositories with no baseline: what the gate *would* reject running cold, not
what it *does* reject on an edit. So it answers "does this rule ever fire alone", which is
the `fit_when` question, and it is **not** a false-positive rate and not a fix rate. Google's
Tricorder gates a build-breaking analyzer on an effective false-positive rate of essentially
zero and puts an analyzer whose findings are marked unhelpful >=10% of the time on probation
(Sadowski et al., ICSE-SEIP 2015) -- those are click rates on shown findings, and the
equivalent for OXN is per-rule repair data from `benchmarks/dogfood-log.jsonl`, which is the
right shape and still too small.

Measured 2026-09-10:

    rule                          caught  unique
    function_sloc                   1386     952
    cognitive_complexity             678     147
    cyclomatic_complexity            351      98
    file_sloc                        272     272
    methods_per_class                102      30
    weighted_methods_per_class        98      26
    parameter_count                   95      78
    max_nesting_depth                 25       0
    shredding                          3       1
"""

import sqlite3
from pathlib import Path

from oxn import thresholds
from oxn.config import GATED_METRICS

CORPORA = Path("benchmarks/corpora")
BEDS = (
    ("python-httpx", "python"),
    # Added 2026-09-18 with `measure_ceilings.py`'s copy of this list. The two scripts keep
    # separate bed tuples and a corpus added to one was silently absent from the other, so
    # `calibration.py` would have cited "seven corpora" for a ceiling's cost and "six" for
    # whether that ceiling catches anything unique -- two populations behind one argument.
    ("python-airflow", "python"),
    ("go-kit", "go"),
    ("rust-ripgrep", "rust"),
    ("rust-tokio", "rust"),
    ("java-spring-petclinic", "java"),
    ("typescript-nest", "typescript"),
    ("javascript-eslint", "javascript"),
)
EXCLUDED = {"javascript-eslint": ("docs/", "tests/bench/", "tests/performance/")}


def _language_of(path: str) -> str:
    from oxn.profiles import profile_for_path

    profile = profile_for_path(path)
    return profile.name if profile is not None else ""


def violators(db: Path, rule: str, language: str, excluded: tuple[str, ...]) -> set[str]:
    """Entity ids over the ceiling for one rule, in this language's files."""
    gate = GATED_METRICS[rule]
    ceiling = float(getattr(thresholds, gate.threshold))
    kinds = ",".join("?" * len(gate.kinds))
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT m.entity_id, m.value, e.file_path FROM metrics m"
            " JOIN entities e ON e.id = m.entity_id"
            f" WHERE m.metric_key = ? AND e.kind IN ({kinds})",
            (gate.metric, *gate.kinds),
        ).fetchall()
    return {
        entity
        for entity, value, path in rows
        if value > ceiling and _language_of(path) == language and not path.startswith(excluded)
    }


RULES = [r for r in GATED_METRICS if r != "file_sloc"]  # file_sloc governs files, not callables

found: dict[str, set[str]] = {rule: set() for rule in GATED_METRICS}
for corpus, language in BEDS:
    db = CORPORA / corpus / ".oxn/cache/graph.db"
    for rule in GATED_METRICS:
        for entity in violators(db, rule, language, EXCLUDED.get(corpus, ())):
            found[rule].add(f"{corpus}:{entity}")

print(f"{'rule':30}{'caught':>8}{'unique':>8}   also caught by")
for rule, caught in sorted(found.items(), key=lambda kv: -len(kv[1])):
    others = set().union(*(found[other] for other in found if other != rule)) if found else set()
    unique = caught - others
    overlap = sorted(
        (other, len(caught & found[other]))
        for other in found
        if other != rule and caught & found[other]
    )
    shown = ", ".join(f"{name} {count}" for name, count in sorted(overlap, key=lambda p: -p[1])[:3])
    print(f"{rule:30}{len(caught):>8}{len(unique):>8}   {shown}")
