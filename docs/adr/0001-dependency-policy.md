---
id: ADR-0001
title: Dependency policy — free-only, self-implement the rest
status: accepted
date: 2026-08-28
tags: [dependencies, licensing, architecture, cost]
applies-to: ["**"]
---

# ADR-0001: Dependency policy

## Status

Accepted — 2026-08-28

## Context

OXN computes software architecture and quality metrics. The obvious implementation shortcut is to
shell out to existing analysers: radon for Python complexity, gocyclo for Go, ESLint for
TypeScript, SonarQube or Sonargraph for architecture metrics.

Two constraints make that shortcut unattractive.

**Cost.** The project is OSS / portfolio / internal tooling and must never require payment. Every
tool that does *architecture* metrics well — Sonargraph, Structure101, NDepend, Understand, Lattix,
CAST, CodeScene — is commercial. CodeQL is free only for open-source repositories. SonarQube's
architecture and branch features live above the free Community Build line.

**Operations.** OXN runs inside a Claude Code `PostToolUse` hook, on every agent edit. The latency
budget is roughly 50 ms p95 for a single file. Subprocess boundaries to Python, Go, Node and JVM
tools cost 200–1500 ms each in startup alone, before any analysis. Worse, those tools disagree with
each other: radon's cyclomatic complexity for Python and gocyclo's for Go differ on `and`/`&&`,
comprehensions, and `else` handling. A configured ceiling of 10 would silently mean something
different in each language.

## Decision

**Admissibility rule.** A third-party component may be a *runtime* dependency only if all four hold:

1. **$0 forever**, for any use including private and commercial repositories. No seat licences, no
   license server, no "free for open source only" tier.
2. **Permissive licence** compatible with OXN's MIT: MIT, Apache-2.0, BSD, ISC. MPL-2.0 is tolerable
   at arm's length (subprocess, not linked). GPL, AGPL and LGPL are excluded from runtime.
3. **Installs via `pip` or a single self-contained binary.** No JVM server, no database, no daemon.
4. **Fully offline.** No telemetry, no account, no network call at analysis time.

Anything failing these is **self-implemented**, or demoted to **dev/CI-only** use.

**Consequent classification.**

*Adopted at runtime:*
- `tree-sitter` + `tree-sitter-language-pack` (MIT; 371 grammars, fetched lazily on first use) — parsing.
- `pydantic`, `typer`, `pyyaml`, `rich` — plumbing.
- MCP Python SDK / `fastmcp` (MIT) — protocol.

*Adopted as optional extras:*
- SCIP indexers — `scip-python`, `scip-typescript`, `scip-java`, `scip-go`, `rust-analyzer --scip`
  (Apache-2.0) — name resolution. See ADR-0002.
- stdlib `sqlite3` for Code Graph persistence — zero dependency, transactional, cold-hook friendly.
- LSP servers via a small client — resolution fallback.
- `model2vec` (MIT) under `oxn[semantic]` — static embeddings without torch.
- Joern (Apache-2.0), Z3 (MIT), clingo (MIT), Soufflé — research track only, never on the hook path.

*Self-implemented:*
- Every metric. All architecture metrics (Martin, DSM, propagation cost, Lakos levelization, cycles,
  architectural smells) because the good tools are commercial. All complexity metrics because the
  free tools are per-language fragments that cannot give uniform semantics at hook latency.
- Graph algorithms (Tarjan SCC, transitive closure, DSM operations) — ~250 LOC total, no `networkx`.
- BM25 retrieval — ~60 LOC, no `rank-bm25`.
- Change-coupling and hotspot analysis from `git log` — no `code-maat` (AGPL).

*Dev / CI only, as differential-test oracles:*
radon, lizard, complexipy, gocognit, eslint-plugin-sonarjs, rust-code-analysis, PMD, Checkstyle,
pydeps, dependency-cruiser, madge, tach, import-linter, jdeps, ArchUnit, `networkx`, SonarQube
Community Build. These never ship as runtime dependencies; they exist to prove our numbers are right.

*Excluded entirely:*
SonarQube Developer/Enterprise, SonarCloud paid tiers, CodeScene, Sonargraph Architect and its
"Zügel" MCP, Structure101 / Restructure101, NDepend, Understand / SciTools, Lattix, CAST, Designite
paid tiers, Codacy, DeepSource, CodeQL, code-maat.

## Consequences

**Positive.** Uniform metric semantics across every language. One `pip install`, no Go/Node/JVM
toolchain requirement — which is OXN's entire time-to-value wedge. Hook-path latency stays inside
budget. No licence can be revoked or repriced underneath the project.

**Negative.** Substantially more code to write and maintain, and full responsibility for
correctness. Mitigated by making the CI differential-oracle harness (Phase P2) non-negotiable and
permanent: our implementations are continuously checked against the free tools we chose not to
depend on.

**Neutral.** Two dependencies remain where the work is genuinely commodity — parsing and name
resolution. Rebuilding tree-sitter, or Python/TypeScript/Java type resolution, would be years of
work for no differentiation.
