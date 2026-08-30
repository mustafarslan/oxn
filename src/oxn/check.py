"""`oxn check` — the gate. The one command a hook calls and CI depends on.

Everything else OXN exposes reports; this one *decides*. Three properties make that decision
trustworthy, and each is enforced here rather than assumed:

**Only a sound measurement may block.** ADR-0002's gate policy: a value blocks a ceiling
when it is `EXACT`, or when it is approximate *and known to be a lower bound* — a lower
bound already over the ceiling proves the true value is too. Anything else is reported as
advisory. `MetricValue.can_block_ceiling` is the single place that judgement lives.

**A finding's identity survives editing.** Baselining is worthless if adding a line above a
function re-reports it as new, so a finding is identified by rule, path and qualified name —
never by line number, which is carried for humans and ignored by the ratchet.

**The baseline is a ratchet, not an amnesty.** A recorded violation stops failing the build;
the *same* violation getting worse starts failing it again. Without that second half a
baseline is a way to switch the gate off one entry at a time.

Two scopes, because they have different budgets. The default is file-scoped — Tier-1
ceilings on the paths given — and belongs in a `PostToolUse` hook at ~100 ms. `--deep` adds
the repository-scoped architectural tier (cycles, layer contracts), which needs the whole
import graph and belongs in CI. The `scope` field in the JSON says which ran, so a baseline
is never compared against a different question from the one that produced it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from oxn.config import Config

#: Exit codes. 2 is what a Claude Code `PostToolUse` hook must return to feed its output
#: back to the agent as something to act on; 1 is reserved for OXN failing to run at all.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_VIOLATIONS = 2


@dataclass(frozen=True, slots=True)
class Finding:
    """One violation, with everything needed to act on it or to baseline it."""

    rule: str
    path: str
    entity: str
    line: int
    value: float
    ceiling: float
    #: False when the measurement is not sound enough to block (ADR-0002). Such findings are
    #: reported, counted, and never fail a build.
    blocking: bool = True
    explanation: tuple[str, ...] = ()
    detail: str = ""

    @property
    def key(self) -> str:
        """Stable identity across edits: no line number, because lines move and rules do not."""
        return f"{self.rule}|{self.path}|{self.entity}"

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "rule": self.rule,
            "path": self.path,
            "entity": self.entity,
            "line": self.line,
            "value": self.value,
            "ceiling": self.ceiling,
            "blocking": self.blocking,
            "message": str(self),
        }
        if self.explanation:
            payload["explanation"] = list(self.explanation)
        if self.detail:
            payload["detail"] = self.detail
        return payload

    def __str__(self) -> str:
        if self.detail:
            return f"{self.path}:{self.line} {self.entity}: {self.detail}"
        return (
            f"{self.path}:{self.line} {self.entity} has {self.rule} "
            f"{self.value:g}, above the ceiling of {self.ceiling:g}"
        )


@dataclass
class CheckReport:
    """What one `oxn check` run decided, and why."""

    scope: str = "files"
    paths: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    #: Findings the baseline already knows about at this severity or worse.
    baselined: list[Finding] = field(default_factory=list)
    #: Baselined findings that got *worse*. These fail: that is the ratchet.
    regressed: list[Finding] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.blocking]

    @property
    def advisory(self) -> list[Finding]:
        return [finding for finding in self.findings if not finding.blocking]

    @property
    def passed(self) -> bool:
        return not self.blocking and not self.regressed and not self.errors

    @property
    def exit_code(self) -> int:
        if self.errors:
            return EXIT_ERROR
        return EXIT_OK if self.passed else EXIT_VIOLATIONS

    def as_dict(self) -> dict[str, Any]:
        from oxn import __version__

        return {
            "oxn_version": __version__,
            "status": "PASSED" if self.passed else "FAILED",
            "scope": self.scope,
            "paths": self.paths,
            "violations": [finding.as_dict() for finding in self.blocking],
            "regressions": [finding.as_dict() for finding in self.regressed],
            "advisory": [finding.as_dict() for finding in self.advisory],
            "baselined": len(self.baselined),
            "diagnostics": self.diagnostics,
            "errors": self.errors,
        }


def run_check(
    paths: list[str],
    *,
    config: Config | None = None,
    deep: bool = False,
    use_baseline: bool = True,
) -> CheckReport:
    """Measure `paths` and decide. Never raises for a *code* problem — only for its own."""
    from oxn.config import Config as _Config

    settings = config or _Config.load()
    targets = [Path(raw) for raw in (paths or ["."])]
    report = CheckReport(scope="repository" if deep else "files")

    missing = [str(target) for target in targets if not target.exists()]
    if missing:
        report.errors = dict.fromkeys(missing, "no such file or directory")
        return report

    findings = _measure(targets, settings, report)
    if deep:
        findings.extend(_architecture(targets, settings, report))

    baseline = _load_baseline(settings.baseline_path) if use_baseline else {}
    report.findings, report.baselined, report.regressed = _apply_baseline(findings, baseline)
    return report


def _measure(targets: list[Path], settings: Config, report: CheckReport) -> list[Finding]:
    """Tier-1 ceilings over every entity in `targets`. The hook path, and the whole budget."""
    from oxn.graph.contracts import assign_layers
    from oxn.graph.indexer import Indexer
    from oxn.graph.sources import iter_source_files

    findings: list[Finding] = []
    with Indexer() as indexer:
        index = indexer.index(targets)
        report.errors.update(index.errors)
        wanted = [indexer.relative(path) for path in iter_source_files(targets)]
        report.paths = wanted
        layer_of = assign_layers(wanted, settings.layers) if settings.layers else {}

        for path in wanted:
            if settings.is_advisory(path):
                continue
            entities = {entity.id: entity for entity in indexer.store.entities_for(path)}
            for entity_id, values in indexer.store.measurements_for(path).items():
                entity = entities.get(entity_id)
                if entity is None:  # pragma: no cover - a store this stale cannot be trusted
                    continue
                findings.extend(
                    _ceiling_findings(path, entity, values, settings, layer_of.get(path))
                )
    return findings


def _ceiling_findings(
    path: str, entity: Any, values: dict[str, Any], settings: Config, layer: str | None
) -> list[Finding]:
    from oxn.config import GATED_METRICS

    findings = []
    kind = entity.kind.value
    for rule in settings.ceilings:
        gate = GATED_METRICS[rule]
        # A ceiling means something only for the kinds its rule is about: 60 lines is a
        # statement about a function, and applying it to a module flags every file there is.
        if kind not in gate.kinds:
            continue
        measured = values.get(gate.metric)
        if measured is None:
            continue
        limit = settings.ceiling_for(rule, layer)
        if measured.value <= limit:
            continue
        findings.append(
            Finding(
                rule=rule,
                path=path,
                entity=entity.qualified_name,
                line=entity.start_line,
                value=measured.value,
                ceiling=limit,
                # ADR-0002: an unsound measurement is reported, never enforced.
                blocking=measured.can_block_ceiling,
                explanation=measured.explanation,
            )
        )
    return findings


def _architecture(targets: list[Path], settings: Config, report: CheckReport) -> list[Finding]:
    """Repository-scoped contracts. Needs the whole import graph, so it is never on the hook."""
    if not settings.contracts:
        report.diagnostics.append("no contracts declared in oxn.yaml; architectural check skipped")
        return []
    from oxn.graph.contracts import check_contracts
    from oxn.graph.depgraph import build_dependency_graph
    from oxn.graph.indexer import Indexer
    from oxn.graph.sources import iter_source_files

    with Indexer() as indexer:
        files = list(iter_source_files(targets))
        graph = build_dependency_graph(indexer.root, files)
        # `check_contracts` wants the file-level graph: a layer is a set of paths, and a
        # violation has to name the file that caused it, not the directory it sits in.
        conformance = check_contracts(graph.files, settings.layers, settings.contracts)

    # Keyed by the offending *edge*, never by the layer pair. `Violation.source` is a layer
    # name, and a finding keyed on it forgives every later import between the same two
    # layers -- so a brand-new violation would land on an existing baseline entry with an
    # identical value, and neither "new" nor "worse" could ever fire. The baseline would
    # look like a ratchet and behave like an amnesty.
    return [
        Finding(
            rule=f"contract:{violation.contract}",
            path=violation.chain[0] if violation.chain else violation.source,
            entity=f"{violation.chain[0]} -> {violation.chain[-1]}"
            if violation.chain
            else f"{violation.source} -> {violation.target}",
            line=1,
            value=1.0,
            ceiling=0.0,
            detail=str(violation),
        )
        for violation in conformance.divergent
    ]


# ---- the baseline ---------------------------------------------------------------------


def _load_baseline(path: Path) -> dict[str, float]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    entries = raw.get("violations", {}) if isinstance(raw, dict) else {}
    return (
        {key: float(value) for key, value in entries.items()} if isinstance(entries, dict) else {}
    )


def _apply_baseline(
    findings: list[Finding], baseline: dict[str, float]
) -> tuple[list[Finding], list[Finding], list[Finding]]:
    """Split findings into (still failing, forgiven, forgiven-but-worse).

    The third list is the ratchet, and it is the half that makes a baseline honest: a
    recorded violation may stay, but it may not grow.
    """
    failing: list[Finding] = []
    forgiven: list[Finding] = []
    regressed: list[Finding] = []
    for finding in findings:
        recorded = baseline.get(finding.key)
        if recorded is None:
            failing.append(finding)
        elif finding.value > recorded:
            regressed.append(finding)
        else:
            forgiven.append(finding)
    return failing, forgiven, regressed


def write_baseline(report: CheckReport, path: Path) -> int:
    """Record every current violation so only *new* ones fail. Returns how many were recorded."""
    from oxn import __version__

    recorded = {
        finding.key: finding.value
        for finding in [*report.findings, *report.baselined, *report.regressed]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "oxn_version": __version__,
                "scope": report.scope,
                "note": (
                    "Accepted debt: these violations do not fail the build, but they may not "
                    "get worse, and a new one anywhere fails. Regenerate with `oxn baseline`."
                ),
                "violations": dict(sorted(recorded.items())),
            },
            indent=2,
        )
        + "\n"
    )
    return len(recorded)


# ---- human-readable output ------------------------------------------------------------


def render(report: CheckReport, console: Any) -> None:
    """Print a check report for a person.

    `rich` is imported by the caller, never here: this module is on the hook's fast path,
    and `cli.py` exists to keep that path free of the presentation layer.
    """
    if report.errors:
        for path, problem in report.errors.items():
            console.print(f"[red]error[/red] {path}: {problem}")
        return

    for finding in report.regressed:
        console.print(f"[red]regressed[/red] {finding}")
    for finding in report.blocking:
        console.print(f"[red]violation[/red] {finding}")
    for finding in report.advisory:
        console.print(f"[yellow]advisory[/yellow]  {finding} [dim](not exact enough to gate)[/dim]")

    for note in report.diagnostics:
        console.print(f"[dim]{note}[/dim]")

    counts = (
        f"{len(report.paths)} file(s) · {len(report.blocking)} violation(s)"
        f" · {len(report.regressed)} regression(s)"
    )
    if report.baselined:
        counts += f" · {len(report.baselined)} baselined"
    if report.advisory:
        counts += f" · {len(report.advisory)} advisory"
    console.print(f"[dim]{counts}[/dim]")
    console.print("[green]passed[/green]" if report.passed else "[red]failed[/red]")
