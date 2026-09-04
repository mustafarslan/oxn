"""The constraint bundle: the task-relevant subset, typed, ranked and capped.

[ADR-0006](../../../docs/adr/0006-retrieval-and-budgeting.md) section 4. Three rules decide
what a bundle is, and each of them is a response to a measured or published result rather
than a preference:

* **Scope partitions, text orders within it.** A constraint governing a file the task is
  about comes before one that does not, whatever the words say -- because "fix the parser"
  cannot be expected to name the ceiling it is about, and section 5a measured what happens
  when text is asked to decide alone (P@1 0.377 against 0.362 for chance).
* **With no target files, breadth decides.** There is no scope to partition by, so the
  ordering falls back to how much of the tree each constraint governs -- the one signal
  that beat the text ranker outright on OXN's own labels. It is emitted as a number the
  agent can see (`governs`) rather than folded invisibly into a score.
* **The cap is a display policy, never an enforcement one.** `omitted` is always reported,
  and `note` says the gate still checks what was not shown. Constraint decay (arXiv
  2605.06445) is why the cap exists; pretending the hidden constraints do not apply is how
  a cap would become a lie.

`enforced` is the field to read first. A ceiling or a contract compiles to a rule the gate
evaluates; a decision without machine-checkable constraints is prose the gate cannot check,
and saying so is more useful to an agent than quietly listing both together.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from oxn import thresholds
from oxn.context.bm25 import BM25, Document

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable, Sequence

    from oxn.config import Config
    from oxn.graph.contracts import Contract
    from oxn.rules.adr import Decision

#: Said in the bundle itself, because an agent that infers "seven constraints" means "seven
#: constraints exist" has been misled by the budget rather than helped by it.
NOTE = "The gate enforces every constraint, shown or not. This bundle is ranked and capped."


class Constraint(BaseModel):
    """One invariant, in the machine-parseable form ADR-0006 section 4 requires."""

    name: str
    #: `ceiling`, `contract` or `decision`.
    kind: str
    #: The invariant itself, as a statement rather than a paragraph.
    statement: str
    #: Path globs this governs. Empty means "declared, but matching nothing here".
    scope: tuple[str, ...] = ()
    #: Where it came from: `oxn.yaml`, or the ADR's path.
    source: str = ""
    #: Whether the gate can actually check it. False for prose, and worth saying plainly.
    enforced: bool = True
    #: Files in the repository this governs -- the breadth prior, and the tiebreak.
    governs: int = 0
    #: BM25 score against the task text. Comparable within a bundle, never across bundles.
    relevance: float = 0.0
    #: The provenance text ranked against the task. Never emitted: it is the ADR's whole
    #: body, which is the thing a bundle exists to avoid pasting at an agent.
    text: str = Field(default="", exclude=True, repr=False)


class Bundle(BaseModel):
    """What `get_architectural_context` will return (P9). A JSON Schema comes free."""

    task: str = ""
    targets: tuple[str, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    #: How many constraints existed but were not shown. Never hidden.
    omitted: int = 0
    note: str = NOTE


@dataclass(frozen=True, slots=True)
class Project:
    """Everything a bundle is built from, so that building one is a pure function."""

    config: Config
    decisions: tuple[Decision, ...] = ()
    #: Repo-relative paths, for the breadth prior. The caller walks the tree; this does not.
    paths: tuple[str, ...] = ()


def build_bundle(
    project: Project,
    *,
    task: str = "",
    targets: Sequence[str] = (),
    limit: int = thresholds.MAX_BUNDLE_CONSTRAINTS,
) -> Bundle:
    """Rank every constraint this project declares, and return the top `limit`."""
    candidates = _annotate(_candidates(project), project.paths, task)
    ordered = sorted(candidates, key=lambda c: _key(c, targets))
    return Bundle(
        task=task,
        targets=tuple(targets),
        constraints=tuple(ordered[:limit]),
        omitted=max(0, len(ordered) - limit),
    )


def _key(constraint: Constraint, targets: Sequence[str]) -> tuple[bool, float, int, str]:
    """Scope, then text, then breadth -- and breadth changes sign depending on the first.

    With targets, the covering constraints have already been selected, so the *narrower* of
    two that both govern this file is the more informative: a ceiling that applies to every
    file in the repository is ambient, while one scoped to the area being edited exists
    because of it. Without targets there is nothing to be specific about, and the ordering
    inverts into the breadth prior ADR-0006 section 4 describes -- the one signal that beat
    the text ranker on OXN's own labels.
    """
    direction = 1 if targets else -1
    return (
        not _covers(constraint.scope, targets),
        -constraint.relevance,
        direction * constraint.governs,
        constraint.name,
    )


def _candidates(project: Project) -> list[Constraint]:
    return [
        *_ceilings(project.config),
        *_contracts(project.config),
        *_decisions(project.decisions),
    ]


def _annotate(candidates: list[Constraint], paths: Sequence[str], task: str) -> list[Constraint]:
    """Fill in `governs` and `relevance`. Identifiers are positions, so they are unique
    even when two constraints share a name across sources."""
    index = BM25(Document(str(position), c.text) for position, c in enumerate(candidates))
    scores = {hit.identifier: hit.score for hit in index.rank(task)} if task else {}
    return [
        candidate.model_copy(
            update={
                "governs": sum(1 for path in paths if _covers(candidate.scope, [path])),
                "relevance": scores.get(str(position), 0.0),
            }
        )
        for position, candidate in enumerate(candidates)
    ]


def _covers(scope: Sequence[str], paths: Sequence[str]) -> bool:
    return any(fnmatch(path, pattern) for path in paths for pattern in scope)


def _ceilings(config: Config) -> list[Constraint]:
    """Project ceilings, then per-layer overrides. Both are `oxn.yaml`'s to state."""
    patterns = {layer.name: layer.patterns for layer in config.layers}
    project = [
        Constraint(
            name=rule,
            kind="ceiling",
            statement=f"{rule} <= {value:g}",
            scope=("**",),
            source="oxn.yaml",
            text=f"{rule} ceiling limit maximum {value:g}",
        )
        for rule, value in sorted(config.ceilings.items())
    ]
    return project + [
        Constraint(
            name=f"{rule} in {layer}",
            kind="ceiling",
            statement=f"{rule} <= {value:g} within layer {layer}",
            scope=patterns.get(layer, ()),
            source="oxn.yaml",
            text=f"{rule} ceiling limit maximum {value:g} layer {layer}",
        )
        for layer, overrides in sorted(config.layer_ceilings.items())
        for rule, value in sorted(overrides.items())
    ]


def _contracts(config: Config) -> list[Constraint]:
    patterns = {layer.name: layer.patterns for layer in config.layers}
    return [
        Constraint(
            name=contract.name,
            kind="contract",
            statement=_contract_statement(contract),
            scope=tuple(
                pattern for layer in _layers_of(contract) for pattern in patterns.get(layer, ())
            ),
            source="oxn.yaml",
            text=f"{contract.name} {contract.kind} {_contract_statement(contract)}",
        )
        for contract in config.contracts
    ]


def _layers_of(contract: Contract) -> list[str]:
    named = [*contract.order, *contract.forbidden, *contract.modules]
    return [*named, contract.package] if contract.package else named


def _contract_statement(contract: Contract) -> str:
    """The contract as one line, in the vocabulary `oxn.yaml` uses for it."""
    if contract.kind == "layered":
        return f"layers {' -> '.join(contract.order)}: a layer may import only those after it"
    if contract.kind == "forbidden":
        return f"{contract.source} may not import {', '.join(contract.forbidden)}"
    if contract.kind == "independence":
        return f"{' and '.join(contract.modules)} may not import each other"
    entrypoints = ", ".join(contract.allowed_entrypoints) or "none"
    return f"{contract.package} may be imported only through {entrypoints}"


def _decisions(decisions: Iterable[Decision]) -> list[Constraint]:
    """Every accepted decision, plus one constraint per ceiling it tightens.

    The decision itself is `enforced=False` even when it tightens a ceiling: the tightening
    is emitted separately as the thing the gate checks, and the prose around it is not.
    """
    out: list[Constraint] = []
    for decision in decisions:
        if not decision.enforceable:
            continue
        body = f"{decision.title}\n{' '.join(decision.tags)}\n{decision.body}"
        out += [
            Constraint(
                name=f"{rule} ({decision.identifier})",
                kind="ceiling",
                statement=f"{rule} <= {value:g}",
                scope=decision.applies_to,
                source=decision.path,
                text=body,
            )
            for rule, value in sorted(decision.ceilings.items())
        ]
        out.append(
            Constraint(
                name=decision.identifier,
                kind="decision",
                statement=decision.title,
                scope=decision.applies_to,
                source=decision.path,
                enforced=False,
                text=body,
            )
        )
    return out
