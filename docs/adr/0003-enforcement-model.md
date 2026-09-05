---
id: ADR-0003
title: Enforcement model — hooks enforce, MCP informs, constraints are budgeted
status: accepted
date: 2026-08-28
tags: [mcp, hooks, agents, enforcement, constraints]
applies-to: ["src/oxn/check.py", "src/oxn/init.py", "src/oxn/cli.py", "src/oxn/_app.py", "src/oxn/retry.py"]
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

## Amendment, 2026-09-05 — the payload was never delivered, and the loop is now bounded

Two P9 items landed together, because the first one had to be true before the second could
mean anything.

### The diagnostics went to the wrong stream, for the whole of P9

Decision 1 above says the hook "returns exit code 2 with exact diagnostics on violation".
It returned exit code 2. The diagnostics went to **stdout**.

Claude Code's hook contract is explicit, and it is the opposite way round: for
`PostToolUse`, exit code 2 *"shows stderr to Claude; the tool already ran"*, and stdout is
shown to the user in transcript mode. `oxn check --json` wrote every finding — the
byte-precise increment trail this project calls the product — to stdout and nothing at all
to stderr. So what actually reached the agent on a rejected edit was, verbatim:

```
PostToolUse:Write hook blocking error: No stderr output
```

The gate was working perfectly and communicating nothing. An agent could see that *something*
had been rejected and had no way to learn what, which is the exact failure mode
`idea.md` §4 was rewritten to avoid: enforcement that the model cannot act on is enforcement
that changes nothing about the next edit.

Two things are worth recording about how it survived. It is invisible from inside the test
suite — `tests/test_cli.py` asserted exit 2 and asserted the JSON on stdout, and both were
true — and it is invisible from inside a passing `oxn check`, because a human running the
command reads stdout. It was found by making a deliberately bad edit in a real Claude Code
session and reading what came back, which is the *only* place the two streams are told
apart. That is the P9 exit criterion doing its job before it was formally run.

**Corrected.** `oxn.check.remediation()` renders the findings for the agent; `oxn.cli._emit`
writes the JSON to stdout for CI and `check_code` and the prose to stderr for Claude. They
are not the same text in two formats: the JSON carries every field, and stderr leads with
the increment trail and ends with the one instruction the finding implies.

### 4a. The budget is counted per (session, finding), and the halt is a message

Decision 4 said a `retry_budget` caps remediation attempts and left the shape open. It is
now implemented, and three choices in it were not obvious.

**What an attempt is.** A `PostToolUse` hook fires once per edit and remembers nothing, so
the count lives in `.oxn/cache/attempts/<session>.json`, keyed by `session_id` from the hook
payload and by `Finding.key` — which is already line-independent, because the baseline
needed it to be. The count is charged on every report of the same violation and reset by
repairing it. A budget of 3 is therefore spent on the *fourth* sighting: report *n* means
*n − 1* repairs were attempted and none of them worked.

Charging every report, rather than only reports where the value failed to improve, is
deliberate. Not charging on improvement sounds kinder and is unbounded: an agent that
lowers a score by one point per edit never exhausts anything, which is the shape of the
gaming `docs/metrics.md` §10.5 predicts. What the ledger keeps instead is the *trajectory*,
and the halt report prints it — `24 -> 17 -> 17 -> 17` says the agent improved once and then
stalled, which is a different conversation with the user from `24 -> 24 -> 24`.

**Exhaustion changes the message, not the exit code.** A `PostToolUse` hook runs after the
tool and cannot end a turn, undo the write, or stop the agent; there is no exit code that
halts anything. Exit stays 2 — reporting `PASSED` on an unrepaired violation would be an
amnesty, and `oxn baseline` already exists to grant those deliberately and visibly. What
changes is what the agent is told: not another repair request but an instruction to stop,
report what was tried and what the numbers did, and leave the choice between a different
design and `oxn baseline` to the user. **The bound on the loop is a sentence, and this is
the honest limit of the `PostToolUse` channel.** A `PreToolUse` variant — floated in
decision 1 as a later hardening — is the only thing that could enforce it.

**No session means no budget.** A human at a terminal and a CI job send no `session_id`, and
neither should ever halt: three commits touching the same accepted debt is not a failed
repair loop. `retry_budget: 0` disables the bound explicitly, which is the right setting for
CI, where there is no agent to escalate to.
