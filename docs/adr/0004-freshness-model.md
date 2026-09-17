---
id: ADR-0004
title: Freshness — how OXN answers quickly without a daemon
status: accepted
date: 2026-08-30
tags: [performance, mcp, caching, architecture]
applies-to: ["src/oxn/graph/**"]
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

**Open — closed 2026-09-05.** The numbers above are from a 60-file repository. They had to
be re-measured on a large tree before P9 fixed the server's refresh strategy. See the
amendment below: whole-graph analysis *is* seconds rather than milliseconds there, and the
remedy this paragraph proposed turns out not to work.

---

## Amendment, 2026-09-05 — measured at 1,827 files, and the warm process is worth less than this ADR assumed

Re-measured on `typescript-nest` (1,827 TypeScript files, 30x this repository), against the
60-file table above. "Cold process" is what a hook or a CLI invocation pays; "warm process"
is what the MCP server delivers by being alive already.

| operation | 60 files, cold process | 1,827 files, cold process | 1,827 files, warm process |
|---|---|---|---|
| one file, parse + every Tier-1 metric | 76 ms | **88 ms** | **6 ms** |
| whole tree, gate + contracts, warm cache | 78 ms | **2,148 ms** | **1,938 ms** |
| whole dependency graph, cycles, Martin, smells | 127 ms | 1,148 ms | — |
| every class, LCOM + CK suite | 228 ms | 2,501 ms | — |
| the constraint bundle (`get_architectural_context`) | — | 183 ms | **41 ms** |
| whole tree, gate + contracts, **empty cache** | — | **15,489 ms** | — |

**Amendment, 2026-09-17: the table below measures a narrower operation than the hook runs,
and the gap was an excluded subtree being walked.** The row is "one file, parse + every Tier-1
metric"; the hook runs `oxn check --json`, which is the whole gate -- rules engine, layer
contracts, edge facts, baseline. Measured end to end with a `PostToolUse` payload on stdin,
that cost **360 ms median against this ADR's 200 ms p95 budget and 400 ms hard ceiling**.

The cause was not the gate. `_edge_facts` walks the tree on every hook run to resolve imports,
and `exclude` filtered files *after* the walk found them -- so each run descended into
`benchmarks/corpora`, 5,165 directories and 18,259 files, ran `profile_for_path` on every one
and threw them all away by glob. The file set was always right; only the cost was wrong, which
is why it survived: nothing it produced was ever incorrect.

Pruning the subtree instead: **131 ms per walk to 1.3 ms, 4,585 directories visited to 33**,
and the hook path **360 ms to 140 ms median, 150 ms p95** -- inside the budget rather than on
the ceiling. In-process, warm, one file: 328 ms to 62 ms. Pruning is applied only for patterns
ending in `*`, where a directory match guarantees the subtree matches; anything else is
filtered as before, because a directory matching a glob does not mean every file under it does.

**The hook path does not degrade with repository size.** 76 ms at 60 files, 88 ms at 1,827:
a 30x larger tree costs 16%, which is measurement noise around a fixed interpreter startup.
That is mechanism 2 (incremental invalidation) doing exactly what this ADR claims for it,
and it is the number the gate lives or dies on. It is also the reassuring half of this
amendment; the rest is not.

**What the warm process actually buys.** Per-file work: 88 ms to 6 ms, a factor of 14 — and
all of it is interpreter and import startup, which the server pays once per session instead
of once per call. Whole-tree work: 2,148 ms to 1,938 ms, about 10%. The difference is the
finding. Per-file cost is dominated by *starting*; whole-tree cost is dominated by
*computing*, and holding an `Indexer` open does nothing about computation. Measured directly:
three consecutive whole-tree checks in one process took 4,282, 4,244 and 4,284 ms before the
fixes below — the second call is not cheaper than the first, because there is no warm state
left to exploit. A profile of the third puts indexing at 0.74 s of 8.1 s, so the cache is
doing its job; the other 90% is the join and the rule evaluation, and those are recomputed
whatever the cache knows.

**So mechanism 3's remedy does not work, and is not implemented.** "The MCP server should
pre-warm on startup" assumes the expensive thing is cache-fillable. It is not: pre-warming a
cache that is already warm changes nothing about the 1.9 s. Pre-warming helps in exactly one
situation -- the *first* whole-tree call against an empty cache, which is 15.5 s -- and that
is a once-per-clone cost, paid in the background at session start for a report the agent may
never request, by a thread writing to the same SQLite file a hook may be writing to. The
benefit does not carry the complexity. What would actually help the recurring 1.9 s is
memoising the *result* on the tree's content hashes, which is a different mechanism with its
own invalidation story; it belongs to P10 with the rest of the report path, not to P9.

**The measurement found two defects, not just numbers.** Both were invisible at 60 files:

* **The rule engine rebuilt its hash indexes once per rule.** ADR-0005 states that "one
  index per (relation, probed columns) serves the whole evaluation", and `_Indexes` was
  constructed inside `_solve`, which runs per *rule* — so eleven rules probing `metric`
  indexed it eleven times: 6.8 million rows re-indexed per whole-tree run, 72% of the total.
  The test that guards this property evaluates a single rule, and one rule cannot tell the
  two designs apart; there is now a second test at six.
* **`grammar_version()` called `importlib.metadata.version` once per file.** It is part of
  every cache key, and the lookup walks the installed distributions: 1,913 calls, 0.42 s, to
  re-read a string that cannot change while the process is alive.

Together those took the whole-tree check from 4,282 ms to 1,927 ms, with byte-identical
findings (656 on `typescript-nest`, the number ADR-0005's parity tests pin).

**What this does not change.** No daemon, still. The four mechanisms stand; mechanism 3's
*claim* is narrowed from "always current when the agent asks" to what the numbers support:
the server removes the per-call startup tax, which is the whole cost of the per-file
operations an agent actually repeats, and roughly a tenth of the session-scale ones it
does not.

## Amendment, 2026-09-10 — the warm process refreshes the index and never itself

The mechanisms above make the server current about the *tree*. Nothing makes it current
about **OXN**. A `oxn serve` process holds the code it imported at startup for as long as
the agent session lasts, and on a project that gates its own development that code goes
out of date within the hour.

Found from the receiving end, which is the only place it is visible: a call to
`get_architectural_context` came back with

```
ConfigError: 'methods_per_class' is not a gated rule. Known rules: cognitive_complexity,
cyclomatic_complexity, file_sloc, function_sloc, max_nesting_depth, parameter_count, shredding
```

against an `oxn.yaml` that declares `methods_per_class` on line 22. Every word of it was
true of the code the process was holding — the server had been started before the class
aggregates shipped — and false of the code on disk. Nothing in the message said which, so
the only available reading was that the configuration was wrong, and it was not.

That is worse than a stale answer. A stale answer is wrong quietly; this was a **failure
attributed to the repository**, from the tool whose entire purpose is to tell an agent what
the repository requires.

**The mechanism is a stamp, not a reload.** The server records the newest mtime under its
own package at import, and any tool failure is checked against it: if the code on disk has
moved since, the failure carries a line saying so and naming the remedy. Restarting is the
client's business — a stdio server cannot restart itself, and re-importing under a live
session would be worse than the disease.

**And "restart the server" is a coarser instruction than it sounds, tested rather than
assumed.** The stale process was killed on 2026-09-10 to see what would happen: Claude Code
did *not* launch a replacement. The four tools simply left the session, and getting them
back means starting a new one. So the honest remedy is `oxn check` from the shell for an
answer now, and a new session for a current server — which is what the note says, because
telling an agent to "restart the MCP server" invites it to kill the process and lose the
tools it was trying to fix.

**Why only on failure.** A stale server that answers is answering about a version of OXN
that existed, which is the ordinary cost of a warm process and is what `oxn check` from the
shell is for. A stale server that *fails* is the case where the agent has no way to tell
whose fault it is, and that is the one worth spending a line on.

---

## Amendment, 2026-09-10 — one row, three facts

A file's row in the cache is written by three different passes, and "is this current" was
asked as one question with one answer. It has three.

* **Parsed.** Entities and their spans, keyed on the content sha, the profile version and
  the grammar version. This is what the stamp in `files` records.
* **Measured.** Metric rows against those entities. A file row can exist with none:
  `scip.ingest` writes one because symbols and edges need something to reference.
* **Ingested.** SCIP symbols and edges. A file can be parsed and measured and have none,
  which is every file until `oxn index` runs.

Three passes, each correct on its own terms, and each destroying another's work:

1. `oxn index` called `put_file`, which is a delete-and-insert the schema cascades from.
   It took the metrics with it, and the row it left carried the current sha, so `is_current`
   said current and nothing measured the file again. **On `python-httpx`: 0 metric rows, and
   `oxn check .` reporting "60 files, 0 violations, passed" on a corpus with 118.** Fixed by
   `is_current(measured=True)`, which makes "parsed" and "measured" separate questions, and
   by a guard in `ingest_index` against overwriting a row that is already both.

2. That guard sends the indexer back to any file the ingest wrote a row for — and revisiting
   called `put_file`, which cascaded away the symbols and edges the ingest had just written.
   **`oxn index .` wrote 4,176 call edges on `python-httpx` and the next `oxn check .`
   destroyed all 4,176**, leaving `oxn calls` to answer UNAVAILABLE on a tree that had just
   been indexed. Fixed by `_Freshness`: only `STALE` — the stamp itself not matching — may
   replace the row. Keeping it when only measurements are missing is sound *because* the
   stamp matches, so the same bytes, profile and grammar produced it and `build_file` is
   deterministic: the entity ids the new metrics are keyed on are the ones already stored.

3. Not freshness but the same disagreement: `oxn index` did not read `oxn.yaml`'s `exclude`,
   while `oxn check` did, so **`oxn index .` wrote 1,464 benchmark-corpus files into OXN's
   own cache** — 1,646 documents where the gate measures 184.

**The pattern worth naming: every one of these is two commands disagreeing about what the
cache holds, with neither wrong on its own terms.** None was caught by a unit test of either
command, because each command did exactly what it said. They were caught by running two in
sequence and looking at the result — twice by the guard from the previous fix firing in a
new place. A cache shared by passes with different jobs needs its invariants stated over the
*sequence*, not over each writer, and the tests that hold now are sequence tests:
`test_ingesting_an_index_does_not_throw_away_what_was_measured` and
`test_measuring_an_ingested_file_does_not_throw_away_its_call_edges`.

**A metric's own version is part of this and is easy to forget.** Cached measurements are
keyed on `LanguageProfile.version`, so changing what a metric *computes* — not what it reads
— leaves every already-measured file reporting the old number forever. Both metric fixes on
2026-09-10 needed a version bump, and the docstring one was noticed only because the gate
kept reporting a violation that the fixed code no longer produced.
