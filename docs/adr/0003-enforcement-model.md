---
id: ADR-0003
title: Enforcement model — hooks enforce, MCP informs, constraints are budgeted
status: accepted
date: 2026-08-28
tags: [mcp, hooks, agents, enforcement, constraints]
applies-to: ["src/oxn/check.py", "src/oxn/init.py", "src/oxn/cli.py", "src/oxn/_app.py"]
---

# ADR-0003: Enforcement model

## Status

Accepted — 2026-08-28. Supersedes the enforcement design in `idea.md` §4.

## Context

`idea.md` §1.1 argues correctly that soft probabilistic constraints cannot enforce hard invariants —
a model fine-tuned on clean code will still emit a cyclomatic complexity of 15 under prompt
pressure. It then, in §4, enforces via a CLAUDE.md instruction: *"Always call
`verify_code_quality(file_path)`."* That relocates the same weakness rather than removing it. An
MCP tool the model may choose not to call is a soft constraint wearing a deterministic costume.

Three 2026 results sharpen the problem considerably:

- **Constraint decay** (arXiv 2605.06445): as structural constraints accumulate, agent performance
  *degrades* — roughly 30 pp of assertion pass rate lost between unconstrained and fully constrained
  conditions (best case 17 pp, worst 45 pp). More constraints delivered to the agent is not
  monotonically better.
- **Architecture as Capability Equalizer** (arXiv 2608.21747): guidance format matters enormously,
  and inversely with model strength. Machine-parseable contracts took a weak model from 33% to 100%
  route coverage; frontier models barely noticed the format at all.
- **AI-Generated Smells** (arXiv 2605.02741): prompt specificity had *zero* statistical effect on
  architectural decay (p>0.8), while code volume correlated near-perfectly with it (ρ=0.94).

Together: telling the agent more, in prose, does not work; verifying deterministically does; and the
amount you tell it has a cost.

## Decision

**1. Hooks enforce. MCP informs. One engine behind both.**

- A Claude Code `PostToolUse` hook on `Edit`/`Write`/`MultiEdit` runs `oxn check --json` and returns
  exit code 2 with exact diagnostics on violation. This always runs; the model cannot decline it.
  (A `PreToolUse` variant that blocks the write outright is a possible later hardening.)
- The MCP server provides what hooks structurally cannot: `get_architectural_context(module_path,
  task_intent)` — pre-generation, task-conditioned, pull-based. Plus `check_code` for voluntary
  self-checks, `get_metrics`, and `explain_violation`.
- `oxn check --json` is the same engine on the CLI, and in CI.

**2. Constraint bundles are budgeted.** `get_architectural_context` returns the *minimal
task-relevant* constraint set — ranked by relevance and blast radius, and capped — never the whole
configuration. Given constraint decay, dumping every ceiling and forbidden import at the agent is
expected to be actively harmful.

**3. Constraint bundles are machine-parseable.** Typed JSON, optionally emitted as TypeScript
interfaces or a JSON Schema. Never prose paragraphs.

**4. The refactor loop is bounded.** "Clean Code, Better Models" (arXiv 2508.11958) shows LLM
refactoring frequently fails to get under complexity thresholds. A `retry_budget` in `oxn.yaml`
caps remediation attempts; on exhaustion OXN reports clearly rather than looping forever.

**5. CLAUDE.md injection is non-destructive.** `oxn init` writes between managed markers and
preserves existing content. (`idea.md`'s blueprint calls `write_text` and destroys the file.)

## Consequences

**Positive.** Enforcement is genuinely deterministic and un-ignorable. The pre-generation channel,
which hooks cannot provide, is kept where it belongs. Constraint budgeting turns a known failure
mode into a designed-for one — and into the project's most defensible research question.

**Negative.** `PostToolUse` runs *after* the write, so it forces remediation rather than preventing
the bad write. Budgeting means the agent sometimes does not see a constraint that later fires as a
violation — acceptable, because the hook catches it, but it must be measured. Hook wiring is
Claude-Code-specific; other agent hosts get the CLI and MCP surfaces only.

**Open.** Whether constraint budgeting actually mitigates constraint decay is unproven. Phase P10
tests it directly, and a negative result is a publishable and useful outcome.
