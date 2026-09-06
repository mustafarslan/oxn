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

## Amendment, 2026-09-06 — L2' is retired as scoped, and L2 is the work it was standing in for

**L2' has no target in the launch set.** It was specified above as "a small multilspy-based
client **for languages with no SCIP indexer**", and the L2 bullet three lines earlier names a
free, Apache-2.0, offline indexer for every launch language: `scip-python`,
`scip-typescript`, `scip-java`, `scip-go`, `rust-analyzer --scip`. Both sentences have been
in this document since 2026-08-28 and they contradict each other. The set L2' exists to
serve is empty.

What was actually missing was **L2 itself**. `oxn.scip.runner.INDEXERS` held three entries —
Python, TypeScript, JavaScript — so Go, Rust and Java had no L2 at all, and P5's exit
criterion, "L0/L1 precision and recall measured against L2 and **published per language**",
was met for one language out of six. Carrying L2' as the open item made that read like a
gap in an optional fallback rather than a hole in the primary rung.

So: **L2' is retired as specified.** Adding `multilspy` would put a heavy runtime dependency
into the long-lived server — the class of dependency ADR-0001's amendment has just demoted
the MCP Python SDK for being — to serve languages that already have a static-artifact
indexer that is strictly better for OXN's shape. If a language OXN supports later turns out
to have no SCIP indexer, this decision is revisited on that language's evidence; the rung
stays described above as a design, and nothing is built for it speculatively.

### What replaced it, and one operational quirk per indexer

Go and Rust are wired. The argv is now **data on the `Indexer` record** rather than
`if self.language == "python"` with everything else falling through to scip-typescript's
spelling — a default that was correct for exactly the two shapes that existed and wrong for
both new ones: `scip-go` has no `--cwd`, and `rust-analyzer` has no `index` subcommand.

Java stays out, and is *named* as out. `scip-java` is real and free, but it drives the
project's build tool rather than reading source, so it cannot be verified the way the others
were — by running it and reading its `--help`. `oxn doctor` reports it as `n/a` with that
reason, which is a different answer from "not installed" because no command the user runs
will change it.

Two findings from wiring these, both of the kind this ADR's own note about `--project-version`
predicts you only get by running the tool:

* **`scip-go`'s install hint had never worked.** The project moved from
  `github.com/sourcegraph/scip-go` to `github.com/scip-code/scip-go`, and the old path does
  not 404 — `go install` resolves it, downloads it, and *then* refuses because the `go.mod`
  inside declares a different module path. OXN had been printing that command since P5.
* **`scip-go` exits 0 and writes an index when there is no `go.mod`.** It warns on stderr and
  produces a valid, nearly empty SCIP file. `run_indexer`'s success test is that the output
  file exists, so a misconfigured root yields not an error but a silent, empty L2 — every
  coverage number computed from it would be measuring nothing while looking fine.

And the report that made all of this visible was itself wrong: `oxn doctor` kept a **second
copy** of the indexer list, which had drifted into advertising Go, Java and Rust as
installable when `oxn index` could invoke none of them. There is now one registry, in
`oxn.scip.runner`, and `doctor` reads it.

### The gating policy is language-dependent, and was calibrated on one language

`can_block_ceiling` rests on the split above: an L1 answer given *with certainty* may block,
a guess may not. Measuring a second language shows that certainty is not a language-independent
property.

| language | corpus | L2 definition / call coverage | L0/L1 precision when certain | overall precision @ recall |
|---|---|---|---|---|
| Python | httpx | 99% / 96% | **99.8%** | 70.3% @ 100% |
| Go | go-kit | 100% / 88.3% | **88.3%** | 59.3% @ 100% |

An L1-certain call target in Go is wrong about one time in nine, so **Go Tier-3 metrics are
not gate-quality at L1** and must not be treated as though they were. Nothing about the
policy above changes; what changes is the claim that it is satisfied everywhere.

Part of the Go gap was a defect and is fixed. A Go method is a *top-level* declaration
carrying its receiver in the signature rather than in a parent scope, so `go-kit` declares
`With` four times in one file — for `Counter`, `Gauge`, `Timing` and `Histogram`. The
per-file symbol table was `name -> entity`, populated with `setdefault`: it kept the first
and answered every call with it at **confidence 1.0**. `metrics.callgraph` admits exactly
the edges whose confidence is 1.0 below L2, so a one-in-four guess entered the call graph as
ground truth, and every Tier-3 metric downstream inherited it. Keeping every declaration and
reporting `1/n` moved Go from 83.9% to 88.3% and left Python at 99.8%.

The remainder is real and needs receiver *types* — which is exactly what L2 exists to
supply, and why the answer to a weak L1 in a language is that language's SCIP index rather
than a better heuristic.

### Rust and Java need a build, not just a binary

`rust-analyzer scip` is wired and its argv verified, but the binary alone does not work: with
no `cargo` on PATH it panics inside `FetchMetadata::exec` and writes nothing, because it
loads the workspace through cargo rather than reading source. That is the same shape as
`scip-java`'s dependence on Gradle/Maven/sbt. Both are therefore *toolchain* requirements
rather than tool installs, and the install hint says so; `run_indexer` refuses the empty
result rather than reporting an index of nothing.
