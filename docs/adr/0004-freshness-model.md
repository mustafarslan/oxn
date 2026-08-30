---
id: ADR-0004
title: Freshness — how OXN answers quickly without a daemon
status: accepted
date: 2026-08-30
tags: [performance, mcp, caching, architecture]
applies-to: ["oxn/graph/**", "oxn/server.py"]
---

# ADR-0004: Freshness model

## Status

Accepted — 2026-08-30. Refines [ADR-0002](0002-resolution-strategy.md) and supersedes the
"tension to manage" note in `docs/metrics.md` §0.2.

## Context

When a coding agent asks OXN about code quality, the answer must be both **current** and
**fast**. The obvious design is a background watcher: a long-lived process tailing file
changes, keeping a graph and a language server warm, so a query is a lookup rather than a
computation. Tools like codebase-memory-mcp and serena are built that way.

Two things argue against it here.

**The measurements.** With a cold process and the SQLite cache, on this repository:

| operation | cold |
|---|---|
| single file, parse + every Tier-1 metric | **76 ms** |
| whole tree, warm cache, no changes | 78 ms |
| whole dependency graph, cycles, Martin, smells | 127 ms |
| every class, LCOM + CK suite | 228 ms |

The hook's budget is 200 ms p95. A watcher would buy back at most ~200 ms on the slowest,
least frequent path — and buy it with a process to supervise, a staleness race between
watcher and hook, and a new failure mode when the watcher dies quietly.

**The stated design.** `idea.md` §5 promises "zero background daemons", and ADR-0002's
cold-process argument is *why* SCIP beat LSP as the precision layer. Reversing that for
200 ms would be trading a clear architectural property for a marginal gain.

## Decision

**Four mechanisms, in order of when they apply. None is a daemon.**

1. **Content-addressed cache.** Every file's graph and metrics are keyed on
   `(path, content_sha, profile_version, grammar_version, schema_version)`. An unchanged
   file is never re-parsed; a no-op re-run of a 1,900-file tree is 100% cache hits.
2. **Incremental invalidation.** A change invalidates one file, not the tree. This is why
   the hook path is a single-file operation and the report path is a join over cached rows.
3. **The MCP server is the warm process — and it is free.** ADR-0002 already reserves warm
   state for it. While an agent session is running, the server is alive anyway, so it can
   hold the `Indexer` open, refresh incrementally when the agent calls
   `get_architectural_context` or `check_code`, and host the L2′ language server. SQLite is
   in WAL mode, so a cold hook can write while the warm server reads. **This delivers
   "always current when the agent asks" without introducing anything new to run.**
4. **`--watch` is an opt-in optimisation, never a requirement.** If a user wants
   mtime-driven pre-warming, the MCP server can do it behind a flag. The invariant to
   preserve: *OXN requires no daemon; running one is a choice.*

**Staleness is surfaced, never hidden.** Every metric already carries `stale`, and a value
computed from an out-of-date index warns rather than blocks.

## Alternatives considered

**Adopt codebase-memory-mcp or serena as dependencies.** Rejected on identity grounds rather
than quality: OXN *is* a tree-sitter code graph — that is the product — and serena-style LSP
is exactly the L2′ rung already scheduled. Taking them as dependencies would replace the
core with the thing it differentiates from, and would break ADR-0001's install-tax rule.

**A standalone watcher daemon.** Rejected on the numbers above, and revisitable: if a real
repository shows the report path exceeding a second, or if `oxn classes` becomes something
an agent calls per-edit rather than per-session, this decision should be reopened with those
measurements in hand.

## Consequences

**Positive.** No new process to supervise, no watcher/hook race, and the "one `pip install`,
nothing running in the background" promise survives. The warm path arrives with P9 at no
extra architectural cost.

**Negative.** The first query in a session pays full cost — 127 ms for the architecture
graph, 228 ms for the class metrics — where a warm daemon would answer in single-digit
milliseconds. Acceptable because those are session-scale operations, not per-edit ones.

**Open.** The numbers above are from a 60-file repository. They must be re-measured on a
large monorepo before P9 fixes the server's refresh strategy; if whole-graph analysis is
seconds rather than milliseconds there, the MCP server should pre-warm on startup.
