"""`oxn health`: the informational half of the two outputs, which never blocks.

`docs/metrics.md` section 10.1 asks for a gate and a health rating and forbids conflating
them. `check.py` is the gate. This is the other one, and it is a separate module rather than
another function in `report.py` because the file-length ceiling said so on the edit that added
it -- and the split turned out to be real: everything here reads and ranks, nothing decides.

What it reports is a **risk profile** per declared ceiling: the share of source lines sitting
in entities over budget, and the entities carrying that share. `oxn.metrics.profile` records
why there is no 1-5 rating and no composite number on top of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from oxn.render import TO_JSON, Output, emit_health

if TYPE_CHECKING:  # pragma: no cover
    from oxn.graph.indexer import Indexer
    from oxn.metrics.profile import Profile


@dataclass(frozen=True, slots=True)
class _Row:
    """One measured entity, flattened for the profile pass."""

    name: str
    path: str
    line: int
    kind: str
    sloc: float
    by_key: dict[str, float]


def run_health(paths: list[str], output: Output = TO_JSON, *, limit: int = 10) -> dict[str, Any]:
    """Risk profiles per declared ceiling: how much code is over budget, and which code.

    Indexes `paths` first, like every other report: the class aggregates are only correct once
    a whole package has been seen, so this reads the store rather than measuring per file. It
    decides nothing, but it is not read-only -- the cache it warms is the same one `check`
    uses, which is ADR-0004's warm path rather than a side effect.

    A rule that `follows` another is skipped. `shredding` shares the cognitive ceiling and its
    metric exists only on cluster roots, so its denominator is *shred-root lines* while every
    other profile's is all callable lines -- a share of a different population, printed in the
    same column. It is a gate, not a proportion of the codebase.
    """
    from oxn.config import GATED_METRICS, Config

    targets = [Path(raw) for raw in paths]
    missing = [str(target) for target in targets if not target.exists()]
    if missing:
        failure: dict[str, Any] = {
            "status": "ERROR",
            "errors": dict.fromkeys(missing, "no such file or directory"),
        }
        emit_health(failure, output)
        return failure

    settings = Config.load()
    with _indexer(settings) as indexer:
        indexer.index(targets)
        wanted = [indexer.relative(path) for path in indexer.sources(targets)]
        rows = _measured(indexer, wanted)

    profiles = {
        rule: _for_rule(rule, float(ceiling), rows).as_dict(limit=limit)
        for rule, ceiling in sorted(settings.ceilings.items())
        if not GATED_METRICS[rule].follows
    }
    payload: dict[str, Any] = {"status": "OK", "files": len(wanted), "profiles": profiles}
    emit_health(payload, output)
    return payload


def _for_rule(rule: str, ceiling: float, rows: list[_Row]) -> Profile:
    """One declared ceiling's profile over the entities its rule governs.

    The kind guard is the same one `check.py` applies: 60 lines is a statement about a
    function, and a health view that applied it to modules would report every file there is.
    The population is every entity of those kinds that carries the metric -- which is why a
    `follows` rule cannot come through here; see `run_health`.
    """
    from oxn.config import GATED_METRICS
    from oxn.metrics.profile import Measured, profile

    gate = GATED_METRICS[rule]
    governed = [row for row in rows if row.kind in gate.kinds and gate.metric in row.by_key]
    return profile(
        rule,
        ceiling,
        [Measured(r.name, r.path, r.line, r.sloc, r.by_key[gate.metric]) for r in governed],
    )


def _indexer(settings: Any) -> Indexer:
    """An indexer honouring `oxn.yaml`'s `exclude`, exactly as the gate and reports do.

    Which files are ours to measure is the same question here as there: a health view that
    ranks vendored code beside hand-written code reports the vendor's decisions.
    """
    from oxn.graph.indexer import Indexer as _Indexer
    from oxn.graph.store import DEFAULT_CACHE_PATH

    return _Indexer(
        root=settings.root,
        cache_path=settings.root / DEFAULT_CACHE_PATH,
        exclude=settings.exclude,
    )


def _measured(indexer: Indexer, paths: list[str]) -> list[_Row]:
    """Every measured entity in `paths`, read from the store rather than re-measured.

    The class aggregates are only correct package-wide, which `indexer.index` has already
    settled by this point; re-measuring per file here would quietly disagree with the gate.
    """
    rows: list[_Row] = []
    for path in paths:
        entities = {entity.id: entity for entity in indexer.store.entities_for(path)}
        for entity_id, values in indexer.store.measurements_for(path).items():
            entity = entities.get(entity_id)
            if entity is None:
                continue
            by_key = {key: found.value for key, found in values.items()}
            rows.append(
                _Row(
                    name=entity.qualified_name,
                    path=path,
                    line=entity.start_line,
                    kind=entity.kind.value,
                    sloc=by_key.get("sloc", 0.0),
                    by_key=by_key,
                )
            )
    return rows
