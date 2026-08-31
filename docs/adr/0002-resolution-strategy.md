---
id: ADR-0002
title: Name and type resolution — a tiered ladder with stamped outputs
status: accepted
date: 2026-08-28
tags: [resolution, metrics, accuracy, architecture]
applies-to: ["src/oxn/metrics/**", "src/oxn/resolve/**", "src/oxn/scip/**", "src/oxn/graph/**"]
---

# ADR-0002: Resolution strategy

## Status

Accepted — 2026-08-28

## Context

Metrics split cleanly by what program representation they need. Cyclomatic and cognitive complexity
need only a per-function CST. Martin metrics and cycle detection need a module-level import graph.
But cohesion (LCOM), coupling (CBO, RFC), inheritance depth (DIT, NOC), call graphs and dead-code
detection all need real **name and type resolution** — knowing that `self.repo.save(x)` calls
`SqlUserRepository.save` and not some other `save`.

Without resolution these metrics are guesses. Guessed cohesion numbers are worse than no cohesion
numbers, because a gate built on them fires unpredictably and destroys trust in the whole tool.

This matters more than it might seem: the "Modular Mirage" finding (arXiv 2605.02741) — agents
achieving file-level modularity while remaining tightly coupled — is precisely a *cohesion and
coupling* failure. Detecting the documented agent failure mode requires exactly the metrics that
require resolution.

Rebuilding a type checker per language is out of the question. But the hook path cannot afford to
wait on one either.

## Decision

**A four-rung resolution ladder. Every metric output is stamped with the rung that produced it.**

- **L0 — built-in scope resolver.** Our own lexical scope and symbol resolver over the tree-sitter
  CST: scopes, bindings, shadowing, closure and comprehension scopes, imports, module-path
  resolution, `self`/`this` receiver identification, class member tables. Always available, no
  external toolchain, sub-millisecond per file. Approximate for anything needing type inference —
  dynamic dispatch, generics, re-exports. Design reference: scope graphs (Néron, Tolmach, Visser &
  Wachsmuth, ESOP 2015) and stack graphs (Creager & van Antwerpen, EVCS 2023), specifically their
  file-at-a-time construction with cross-file stitching.
- **L1 — project symbol index.** L0's per-file exports and imports stitched into a repo-wide symbol
  table in SQLite: in-repo class hierarchy, module graph, unique-name call resolution.
- **L2 — SCIP index.** `oxn index` invokes the language's SCIP indexer (`scip-python`,
  `scip-typescript`, `scip-java`, `scip-go`, `rust-analyzer --scip`; all Apache-2.0, free, offline),
  ingests the protobuf, and merges compiler-grade symbol identities into the Code Graph. This is what
  makes Tier-3 metrics *exact*. The call graph comes from a **range join** of SCIP occurrences
  against the CST's call nodes, since SCIP itself does not mark which references are calls.
- **L2' — live LSP.** A small multilspy-based client for languages with no SCIP indexer. Accurate but
  needs a warm process, so it lives in the long-lived MCP server and is used on the report path only.
- **L3 — Joern CPG.** Apache-2.0, JVM, minutes. Research track and differential oracle only.

**Why SCIP is the primary precision layer, not LSP.** The `PostToolUse` hook is a **cold process** —
a fresh Python interpreter on every agent edit. It cannot hold a warm language server, a warm JVM, or
a parsed project graph. A SCIP index is a *static artifact on disk*: consuming it is a file read in
milliseconds, not a compilation. LSP's 1–10 second server start makes it structurally incompatible
with the hook, however accurate it is.

**Two latency classes.** The *hook path* (single file, incremental, **p95 ≤ 200 ms**, 400 ms ceiling)
runs at L0/L1 plus whatever L2 index is already cached. The *report path* (`oxn report`, CI, whole
repo) runs at the best available rung. They are never mixed. The budget is tight for a measured
reason: interpreter start alone costs 30–60 ms, and a single top-level `import networkx` costs 146 ms
on the development machine.

**The stamp is part of the contract.** Every metric value carries `resolution_level` and
`exactness ∈ {EXACT, APPROX}`, plus a `stale` flag. The MCP server and CLI surface all three. An
agent must never be told an L0 LCOM is exact. **Gate policy: only `EXACT` and fresh metrics may
block; `APPROX` and `stale` metrics warn.**

**L0/L1 error is measured, not assumed.** The resolution phase publishes L0/L1 precision and recall
against L2 ground truth, per language, per metric. Where L0 is too inaccurate for a metric in a given
language, that metric is simply not reported at L0.

**Honesty clause.** Even at L2, virtual and dynamic dispatch resolve to the *declared* target. CBO
and RFC are EXACT over the resolved symbol set but remain a static approximation of runtime coupling;
call-graph-derived fan-in/out stays APPROX under higher-order functions, reflection, DI containers
and callbacks. This ships in the user-facing docs, not just here.

## Consequences

**Positive.** Tier-3 metrics — LCOM, CBO, RFC, DIT, NOC, call graphs, dead code — become genuinely
exact, which is what lets OXN detect the Modular Mirage rather than merely gesture at it. The hook
stays fast. Honesty about precision is structural rather than a documentation footnote.

**Negative.** The resolution phase is the largest single investment in the roadmap (4–6 focused
weeks, and it could double). L2 requires per-language toolchains, so it is an opt-in `oxn index`
step, never a hard install requirement. Two code paths per resolved metric means more tests.

**Mitigation.** The v0.1 walking skeleton, the VCS tier, and the entire module-graph architectural
gate all ship *before* the resolution phase begins, so the project has a usable, dogfoodable tool
independent of this investment. SCIP ingestion is per-language: shipping Python and TypeScript
resolution while Go and Java are still at L1 is a valid intermediate state.
