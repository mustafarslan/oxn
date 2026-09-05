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

Two scopes, because they have different budgets, and the line between them moved on
2026-09-05. The default is file-scoped — Tier-1 ceilings on the paths given, plus the layer
contracts those paths' own imports can settle — and belongs in a `PostToolUse` hook at
~150 ms on a 1,900-file tree. `--deep` adds what one file cannot answer: edges *into* the
measured files, and cycles. Contracts were `--deep`-only until that date, on the assumption
that any import graph meant parsing the whole tree; resolution in fact needs the tree's
layout and one file's text, so the hook was silent about layer violations for a cost it was
never actually paying.
The `scope` field in the JSON says which ran, so a baseline is never compared against a
different question from the one that produced it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from oxn.config import Config
    from oxn.retry import Attempt

#: Exit codes. 2 is what a Claude Code `PostToolUse` hook returns to feed a message back to
#: the agent; 1 is reserved for OXN failing to run at all.
#:
#: **The message travels on stderr, and for the whole of P9 it did not.** Claude Code's hook
#: contract is explicit -- exit 2 "shows stderr to Claude" -- while `oxn check --json` wrote
#: every finding to *stdout*, which a `PostToolUse` hook shows only in transcript mode. The
#: gate was therefore working perfectly and telling the agent nothing: what actually reached
#: Claude on a rejected edit was the string `No stderr output`. The increment trail is the
#: product, so `remediation()` below renders it to stderr while the JSON stays on stdout for
#: CI and for `check_code`.
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
    #: Retry accounting, populated only on the hook path — a CLI or CI run has no session
    #: to count attempts against, and must never halt asking for a fix.
    attempts: dict[str, Attempt] = field(default_factory=dict)
    #: The budget those attempts are counted against; 0 means no budget applied.
    retry_budget: int = 0

    @property
    def blocking(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.blocking]

    @property
    def advisory(self) -> list[Finding]:
        return [finding for finding in self.findings if not finding.blocking]

    @property
    def failing(self) -> list[Finding]:
        """Everything that fails this build: new violations and baselined ones made worse.

        `regressed` is not in `findings`, so anything counting what the agent has to fix has
        to add it back — a baselined violation the agent keeps making worse is exactly the
        case a retry budget exists for.
        """
        return [*self.blocking, *self.regressed]

    @property
    def exhausted(self) -> list[Finding]:
        """Findings this session has already spent its whole budget failing to repair."""
        return [
            finding
            for finding in self.failing
            if (attempt := self.attempts.get(finding.key)) is not None
            and attempt.exhausted(self.retry_budget)
        ]

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

        payload: dict[str, Any] = {
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
        if self.retry_budget:
            payload["retry"] = {
                "budget": self.retry_budget,
                "attempts": {
                    key: {"count": attempt.count, "values": list(attempt.values)}
                    for key, attempt in sorted(self.attempts.items())
                },
                "exhausted": sorted(finding.key for finding in self.exhausted),
            }
        return payload


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

    findings = _rule_findings(targets, settings, report, deep=deep)

    baseline = _load_baseline(settings.baseline_path) if use_baseline else {}
    report.findings, report.baselined, report.regressed = _apply_baseline(findings, baseline)
    return report


def _indexer(settings: Config) -> Any:
    """An indexer rooted where the config is, and told what the config excludes.

    Both halves matter. `exclude`, `advisory` and `layers` are globs against the project
    root, and a finding's path is two thirds of the key the baseline is stored under -- so an
    indexer rooted somewhere else does not merely mis-filter, it silently resets every
    accepted violation. Rooting it here is what makes `run_check(config=...)` mean anything.
    """
    from oxn.graph.indexer import Indexer
    from oxn.graph.store import DEFAULT_CACHE_PATH

    return Indexer(
        root=settings.root,
        cache_path=settings.root / DEFAULT_CACHE_PATH,
        exclude=settings.exclude,
    )


def _rule_findings(
    targets: list[Path], settings: Config, report: CheckReport, *, deep: bool
) -> list[Finding]:
    """Evaluate the declared rules. This is the gate (ADR-0005).

    `_measure` and `_architecture` below are the hand-coded checks this replaced. They are
    kept deliberately, not pending deletion: they are the oracle for the parity tests, which
    assert byte-identical findings across `src`, two corpora and a four-contract fixture. A
    rule that changes here and not there fails parity loudly (ADR-0005, amended 2026-09-04).
    """
    from oxn.graph.contracts import assign_layers
    from oxn.rules.adr import adr_facts, load_decisions, unscoped_rule
    from oxn.rules.builtin import rules_for_scope
    from oxn.rules.engine import evaluate
    from oxn.rules.facts import file_facts

    with _indexer(settings) as indexer:
        index = indexer.index(targets)
        report.errors.update(index.errors)
        sources = indexer.sources(targets)
        wanted = [indexer.relative(path) for path in sources]
        report.paths = wanted
        layer_of = assign_layers(wanted, settings.layers) if settings.layers else {}
        facts = file_facts(indexer.store, wanted, settings, layer_of)
        populated = _edge_facts(facts, indexer, sources, settings, report)
        rules = rules_for_scope(settings, populated=populated)
        # `--deep` is also what makes "this ADR governs nothing" a meaningful claim: it is
        # a statement about the repository, and `wanted` is the whole tree only here.
        adr_facts(facts, load_decisions(indexer.root), wanted, whole_project=deep)
        if deep:
            rules.append(unscoped_rule())

    return [_as_finding(found) for found in evaluate(rules, facts)]


def _edge_facts(
    facts: Any, indexer: Any, sources: list[Path], settings: Config, report: CheckReport
) -> frozenset[str]:
    """Populate the import relations this run can honestly answer, and say which those are.

    Whether this is the deep scope is read off `report.scope` rather than passed beside it.
    They are the same fact -- `run_check` sets one from the other -- and a signature
    carrying both is a signature in which they can disagree.

    **The hook checks layer contracts too, as of 2026-09-05.** It could not before, and the
    reason was a cost that turned out not to be there: building the graph was assumed to mean
    parsing the whole tree, so contracts went to `--deep` and an agent could import across a
    layer boundary while the hook said nothing, every edit. But resolution needs the tree's
    *layout* — a directory walk, 27 ms over nest's 1,913 files — and only the edited file's
    *text*. So one walk plus one parse buys that file's real edges, and three of the four
    contract kinds are edge joins from the source.

    What this cannot see is stated rather than glossed: only edges **out of** the files
    measured. A file nobody edited importing this one illegally is invisible here and is
    caught by `--deep`, which is also the only scope that can answer `cycle`.
    """
    deep = report.scope == "repository"
    if not settings.contracts:
        if deep:
            report.diagnostics.append(
                "no contracts declared in oxn.yaml; architectural check skipped"
            )
        return frozenset()

    from oxn.graph.depgraph import build_dependency_graph
    from oxn.rules.facts import graph_facts

    # Whole-tree runs resolve against what they parse. The hook parses one file, so it has
    # to be told the layout separately -- and pays for the walk only when a contract exists
    # to spend it on.
    known = None if deep else {indexer.relative(path) for path in indexer.sources()}
    graph_facts(facts, build_dependency_graph(indexer.root, sources, known=known), settings)
    if not deep:
        report.diagnostics.append(
            "file-scoped contract check: edges out of the files measured. "
            "`oxn check --deep` adds edges into them, and cycles."
        )
    return frozenset({"imports", "cycle"}) if deep else frozenset({"imports"})


def _as_finding(found: Any) -> Finding:
    """A `RuleFinding` in the shape the report and the baseline expect."""
    return Finding(
        rule=found.rule,
        path=found.path,
        entity=found.entity,
        line=found.line,
        value=found.value,
        ceiling=found.ceiling,
        blocking=found.blocking,
        explanation=found.explanation,
        detail=found.detail,
    )


def _measure(targets: list[Path], settings: Config, report: CheckReport) -> list[Finding]:
    """Tier-1 ceilings over every entity in `targets`. The hook path, and the whole budget."""
    from oxn.graph.contracts import assign_layers

    findings: list[Finding] = []
    with _indexer(settings) as indexer:
        index = indexer.index(targets)
        report.errors.update(index.errors)
        wanted = [indexer.relative(path) for path in indexer.sources(targets)]
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
        limit = settings.ceiling_for(gate.follows or rule, layer)
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

    with _indexer(settings) as indexer:
        files = indexer.sources(targets)
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


# ---- what the agent is told ------------------------------------------------------------

#: How many findings the hook spells out in full. One edit can break a dozen ceilings at
#: once, and a wall of text is the failure mode constraint decay predicts: the agent reads
#: the top and acts on the top. The rest are counted, and the next edit surfaces them.
MAX_REPORTED = 5


def remediation(report: CheckReport) -> str:
    """What a rejected edit should say to the agent that made it. Plain text, for stderr.

    Not the JSON in a different shape. The JSON on stdout is for CI and for `check_code`,
    which want every field; this is for a model deciding what to do next, so it leads with
    the increment trail -- *which* construct added *how much* at *which line* -- and ends
    with the one instruction the finding implies. `rich` is never imported here for the same
    reason `render` does not import it: this runs on every edit, inside a 200 ms budget.
    """
    if report.errors:
        problems = "\n".join(f"  {path}: {why}" for path, why in sorted(report.errors.items()))
        return f"OXN could not run, so nothing was checked:\n{problems}"

    spent = report.exhausted
    live = [finding for finding in report.failing if finding not in spent]
    sections = [
        section
        for section in (_repair_section(live, report), _halt_section(spent, report))
        if section
    ]
    return "\n\n".join(sections)


def _repair_section(findings: list[Finding], report: CheckReport) -> str:
    """The violations the agent still has budget to fix."""
    if not findings:
        return ""
    shown = [_describe(finding, report) for finding in findings[:MAX_REPORTED]]
    if len(findings) > MAX_REPORTED:
        shown.append(f"  ...and {len(findings) - MAX_REPORTED} more, not listed.")
    return "\n".join(
        [
            f"OXN: this edit breaks {len(findings)} invariant(s). Fix them before moving on.",
            "",
            *shown,
            "",
            "Fix the cause, not the number. Splitting the body into one-line helpers is "
            "detected: rule `shredding` totals a function with the private, trivial helpers "
            "only it calls, so a dedicated helper does not raise the budget.",
        ]
    )


def _describe(finding: Finding, report: CheckReport) -> str:
    """One finding, with the increment trail that says where its score came from."""
    attempt = report.attempts.get(finding.key)
    header = f"  {finding}"
    if attempt is not None and report.retry_budget:
        header += f"  [attempt {attempt.count} of {report.retry_budget}]"
    return "\n".join([header, *(f"    {line}" for line in finding.explanation)])


def _halt_section(findings: list[Finding], report: CheckReport) -> str:
    """The report OXN makes when the loop did not converge (ADR-0003 section 4).

    Deliberately an instruction to *stop and escalate* rather than another repair request.
    A `PostToolUse` hook cannot halt anything by itself -- the tool has already run and the
    exit code does not end the turn -- so the only thing that bounds the loop is telling the
    agent, in as many words, that further attempts are not wanted and why.
    """
    if not findings:
        return ""
    trails = [
        f"  {finding.path}:{finding.line} {finding.entity} — {finding.rule} "
        f"{report.attempts[finding.key].trend} (ceiling {finding.ceiling:g})"
        for finding in findings
    ]
    return "\n".join(
        [
            f"OXN: retry budget spent. {report.retry_budget} repair(s) have not cleared "
            f"{len(findings)} violation(s), and this is what each attempt scored:",
            "",
            *trails,
            "",
            "Stop editing these entities and report to the user. Say what you tried, what "
            "the numbers did, and that the alternatives are a different design or "
            "`oxn baseline`, which records a violation as accepted debt that may not grow. "
            "Choosing between those is theirs, not yours.",
        ]
    )


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
