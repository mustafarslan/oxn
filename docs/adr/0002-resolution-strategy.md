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

> **Superseded the same day, by the fourth amendment below.** `scip-java` was wired and run:
> its argv is a single verified `index --output <path>`, `UNWIRED` is empty, and petclinic is
> measured. What is left of the objection is narrower and real — the invocation depends on
> the build system only when a repository declares *two*, and there OXN refuses rather than
> guesses.

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
not gate-quality at L1**. Nothing about the policy above changes; what changes is the claim
that it is satisfied everywhere.

**How far this reaches, traced rather than assumed — and the first version of this
paragraph overstated it.** It said the wrong answers "can block a Tier-3 ceiling today".
They cannot, and the reason is worth writing down because it is a property that could
silently stop being true.

*No Tier-3 metric is gateable at all.* `GATED_METRICS` is Tier-1 only — the six ceilings
plus `shredding` — and `oxn.yaml` refuses anything else by name: `ceilings: {cbo: 10}` is a
configuration error, not a gate. `build_call_graph` has no production caller, and the only
code creating `CALLS` edges is `scip/join.py`, which is L2. So a wrong L1 answer reaches
**reported** CK numbers (CBO, RFC) and stops there.

Within that, the file-local half is genuinely closed: `_confident_callees` admits only
`is_certain` answers and marks the whole class APPROX if any callee was uncertain, so the
moment an ambiguous name became `1/n` instead of `1.0` those values stopped claiming
exactness — verified on a fixture, not assumed. What remains is 102 of go-kit's 875
confident answers (**11.7%**) that are certain and wrong: the cross-file case, where
`by_file[imported][name]` finds one `Value` in an imported file and answers with full
confidence while Go dispatched on a receiver type L1 never saw. Those overstate a *reported*
CBO or RFC on Go code.

The shape of the fix is known and unbuilt: a call written as a *selector* (`x.Value()`) is
not a bare-name lookup, and resolving it by name alone is a guess — `resolve_call` is not
told which of the two it is handling, and every profile already carries `attribute_kind` to
tell them apart. Until it lands, the honest statement is that **Go Tier-3 numbers are
reported and should not be believed to a tenth**, not that anything is being blocked wrongly.

The protection here is structural rather than remembered, so it is asserted:
`tests/test_tier3.py` fails if any Tier-3 metric is ever added to `GATED_METRICS`, and
points back at this section. That is the line which, if crossed, turns the paragraph above
into the exposure the first draft claimed it already was.

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

## Amendment, 2026-09-06 (second) — monorepo resolution, and how small the gap turned out to be

The module docstring in `graph/resolve.py` has listed "``package.json`` ``exports`` maps,
workspace globs, symlinked monorepo packages" among the things "reported as **external and
unresolved** rather than guessed at" since P4, and the roadmap carried "monorepo support" as
open on that basis. Measuring it first changed what the work was.

**On `typescript-nest` — a real npm-workspaces monorepo and the pinned corpus — all 1,651
cross-package imports already resolved.** Nest declares every `@nestjs/*` package in
`tsconfig.json` `paths`, and OXN has read `paths` since P4. Its graph is 6,271 imports,
5,062 edges, 3 unresolved, and that was true before any of this work. An intermediate
measurement of mine said otherwise and was wrong: it counted *bare specifiers* and never
asked whether they resolved.

What was genuinely missing is the monorepo that declares `workspaces` and no aliases, where
`@scope/pkg` is reachable only through a `node_modules` symlink OXN does not follow and
should not have to. `ResolutionContext` now carries `workspaces: name -> directory`, read
from `package.json#workspaces` (npm, and yarn's older `{"packages": [...]}`),
`pnpm-workspace.yaml#packages`, or `lerna.json#packages` in that order of precedence. The
name comes from each matched directory's own manifest, never from the directory: `packages/
common` calls itself `@nestjs/common`, and it is the name that appears in an import.

`tsconfig` wins where both exist. `paths` is the repository stating where a name resolves;
the workspace layout is an inference from its directory structure, and a repository that
declares both and disagrees meant the one it wrote down.

**The second half is classification, and it is the part that was actively misleading.**
`_is_internal_looking` was `raw.is_relative`, so an unresolvable `@scope/pkg/moved` counted
as a third-party dependency — indistinguishable from `express`, and absent from
`unresolved`. `docs/metrics.md` §4.1 requires a missing edge to be visible; this was the one
class of missing edge that looked exactly like correct behaviour. A bare specifier naming
one of our own packages is now reported when it fails to resolve.

**Out of scope, stated rather than discovered later.** pnpm's `catalog:` and yarn's
`workspace:` protocol specifiers name versions rather than paths. `exports` maps point at
built output (`"./internal": "./internal.js"`) that is not in the source tree OXN measures,
so honouring them would resolve imports to files the repository does not contain — the
directory-plus-probe path finds the source instead, which is what nest's 1,651 already
demonstrate. Python multi-package and Go multi-module layouts are untouched: no pinned
corpus exercises either, and wiring them unverified is the mistake this amendment's
predecessor was written about.

Cost: nest's graph is unchanged and the hook path measures 123 ms against 126 ms before, on
9 workspace packages. Manifest reads are once per `ResolutionContext`, so they scale with
package count rather than file count; a repository with hundreds of packages has not been
measured.

## Amendment, 2026-09-06 (third) — the grader was measuring the wrong population

**The Rust numbers published in the amendment above are withdrawn.** They said 99.1%
confident precision at 73.7% excluded, and they were produced by a defect in
`resolve/measure.py::symbol_tail`, not by the resolver they claimed to describe.

A SCIP symbol is five space-separated fields — `<scheme> <manager> <package> <version>
<descriptors>` — and only the fifth is a descriptor path. `symbol_tail` split the whole
symbol on `/` and `#` and took the last piece. That is correct for Python and Go by
accident: both write the enclosing package as a descriptor containing a `/`, which cuts the
prefix off. `rust-analyzer` writes `rust-analyzer cargo ripgrep 15.2.0
set_windows_exe_options().` for a crate-level function — no `/` anywhere — so the "name"
came back as the entire symbol, and it prefixes a method descriptor with the impl block that
owns it (`[Searcher]search_reader()`), so the bracket group was read as part of the name.

The grader excludes a call site whose oracle symbol does not spell what the source spells,
which is the right rule and the reason httpx's 30.5% is excluded rather than scored. Applied
through a broken parser it excluded **4,844 of ripgrep's 4,905 exclusions in error**: correct
oracle answers, discarded. Rust was graded on 1,747 of 6,652 call sites, and the number
published was that subset's.

Corrected, with Python and Go identical to four decimal places:

| language | confident precision | overall @ recall | excluded | graded |
|---|---|---|---|---|
| Python / httpx | 99.8% | 70.3% @ 100% | 30.5% | 1,453 |
| Go / go-kit | 88.3% | 59.3% @ 100% | 0% | 1,578 |
| Rust / ripgrep | **90.9%** | 51.2% @ 100% | 0.15% | 6,642 |

**The conclusion drawn from the bad numbers is retracted with them.** It held that Go was
the outlier because a Go method is a top-level declaration while Rust's live inside `impl`
blocks. `resolve_call(path, name)` takes a bare name and never consults the impl, so that
distinction buys nothing: `new` is declared in nine `impl` blocks in `line_buffer.rs` alone
and collides exactly as four `With` methods do in one Go file. Rust and Go are the same
shape. **Python's 99.8% is now the thing without an explanation**, and this amendment does
not offer one — httpx's naming and the Python language are not separable from one corpus,
and inventing a cause from one corpus is precisely what produced the paragraph being
retracted here.

The gating policy in this ADR is unchanged and unthreatened: only *certain* answers may
block, `GATED_METRICS` is Tier-1 only, and Tier-1 does not use call resolution.
`metrics.callgraph` and every Tier-3 metric downstream inherit these confidence numbers, are
reported rather than gated, and `tests/test_tier3.py` fails if that ever changes.

**What the grader now asserts about itself.** A grader that cannot parse a language does not
fail — it narrows the population silently and reports a flattering number over what is left.
The only visible symptom is the exclusion rate. `test_oracle_scip.py` bounded it for Python
from the day that test was written and did not for Rust; every corpus now carries its own
ceiling, per-language, because 30.5% is a real property of `scip-python` and nothing near it
is one of `rust-analyzer`.

`symbol_tail` also respects SCIP's backtick quoting now, which is not a detail: a descriptor
is quoted *exactly when* it contains a character that would otherwise be structural, so
splitting without tracking quotes cuts inside the one name that declared it must not be.
ripgrep has `[StderrReader]`r#async`()` — a raw identifier holding a `#` — and the same rule
covers a `]` inside a generic impl owner.

Ten genuine exclusions remain, all `use ... as ...`: the source writes
`generate_version_pcre2`, `rust-analyzer` names the definition `generate_pcre2`. That is the
import-alias gap already recorded as inverted tests for Go in `tests/test_resolve.py`, and
Rust reaching it from the other direction is evidence it is one gap rather than three.

## Amendment, 2026-09-06 (fourth) — Java measured, and what an index costs when it is a build

`java-spring-petclinic`, indexed with `scip-java index --build-tool=maven`: L2 coverage
**99.6% of declarations and 100% of call sites**, the highest of the four languages. L0/L1
scores **100% precision when certain** at 87.9% confident recall, 90.4% overall at 100%
recall, over 324 graded call sites with **zero** untrustworthy exclusions.

**The 100% is a statement about the corpus, not about Java.** 285 confident answers over 324
sites, from 50 files and 4,214 lines; Python, Go and Rust are graded on 1,453, 1,578 and
6,642. A corpus this size cannot distinguish a resolver that is right from one that has not
met a hard case. What it does contain behaves as designed: `findAll` is declared in several
test classes, L1 sees several candidates and reports `1/n` instead of certainty, and all 31
wrong answers fall among the 39 uncertain ones. The Go confidence fix generalises.

**`scip-java` took 361 s, and P5's exit criterion is "index build <60 s on a 100k-LOC
repo".** That is six times the budget on a repository twenty-four times smaller than its
reference size, and it is not a slow indexer: `scip-java` runs Maven, so the wall clock is
dependency resolution and compilation and would be roughly the same for a tenth of the code.
The consequence is a scheduling one rather than a correctness one — **Java L2 is a batch
operation**, not something the hook path or an interactive `oxn index` waits on. The
criterion is met by Python (4.4 s), Go (1 s) and Rust (40 s); TypeScript is wired and
unmeasured, and is not claimed either way.

> **Corrected by the sixth amendment below.** "Installed" was wrong: `scip-typescript` was
> not on this machine at all, which only running it revealed. TypeScript is measured there,
> at 3 s.

**`--build-tool` stays out of the registry, and the earlier reasoning for that was
incomplete.** It said OXN does not guess a build system. The sharper reason is that
detecting the unambiguous case would add nothing at all, because `scip-java` already detects
it and needs no flag; the *only* case the flag decides is the ambiguous one — petclinic,
holding both a `pom.xml` and a `build.gradle` — and that is exactly the case where a guess
runs the wrong build for ten minutes. `scip-java`'s own error names the flag and reaches the
user through `run_indexer`, and `oxn index --index-file` ingests an index the user built by
hand. The oracle test does the same thing a user in that position does, and says so.

**The install hint was wrong in a way only running it reveals.** `coursier install
scip-java` fails — the app is not in the default channel and needs `--contrib` — and
coursier then installs into `~/Library/Application Support/Coursier/bin` without putting it
on PATH, so `shutil.which` reports the tool missing on a machine that has just installed it
successfully. Both are now in the hint, because a hint that leaves the user where they
started is worse than none: it costs them the install *and* the diagnosis.

## Amendment, 2026-09-06 (fifth) — L2 edges pointed at the wrong file, and the rung is the oracle

**A SCIP `local` symbol is document-scoped, and OXN keyed it globally.** The numbering in
`local 0`, `local 1`, … restarts in every document; `typescript-nest`'s index has **628
documents that each define `local 0`**. `symbols.symbol` is a PRIMARY KEY and
`resolve_edge_targets` joins on `s.symbol = edges.dst_ref` with no file condition, so the
last file ingested owned `local 0` and every other file's reference to *its own* `local 0`
resolved into that file.

On nest that manufactured **529 `calls` edges pointing into a file the caller never
mentions** — `packages/common` calling `packages/platform-express` — out of 3,138 resolved
L2 edges. **17% of every L2 edge on that repository was fabricated.**

This is a different class of defect from the grader bug retracted two amendments above, and
worse. That one made a *number about* OXN wrong. This one made OXN's own **L2 store** wrong,
and L2 is the rung the other two are graded against: a fabricated edge here is a fabricated
fact in `metrics.callgraph`, CBO, RFC and dead-code detection alike. It is also why it went
unseen — nothing downstream of L2 has an independent oracle to disagree with it.

The fix is one helper in `scip/join.py`, where SCIP semantics meet OXN entities, rather than
at the store boundary: `scoped(symbol, path)` qualifies a `local` symbol with its document
and leaves every global symbol byte-identical. Definitions, call references and relationship
references all pass through it, so both sides of the join agree. The store, the schema and
`resolve_edge_targets` are untouched, and `measure_corpus` — which had the same collision in
its own `scip_to_entity` map — is fixed by the same change. A same-file local still resolves;
a cross-file one cannot, which is correct, because a local has no cross-file meaning.

Measured after, on all five corpora: **zero** cross-file local resolutions, nest's resolved
edges 3,138 → 2,611, and **every accuracy number identical to four decimal places** — the
graded call sites were all non-local, so the grader was protected from this by accident,
exactly as it was protected from `symbol_tail` by accident. Two accidents in one day is the
argument for the assertions rather than for the care.

Python's 637 exclusions were re-checked against the same suspicion and **none of them is a
local symbol**: the 30.5% figure really is `scip-python`'s re-export bug, and that claim
stands unchanged.

## Amendment, 2026-09-06 (sixth) — TypeScript, and the table completed

`typescript-nest` via `scip-typescript index --output <path> --cwd <root>`: L2 coverage
**99.9% of declarations and 83.6% of call sites**, index built in **3 s**. L0/L1 scores
**99.7% precision when certain** at 53.4% confident recall, 66.7% overall at 100% recall,
over 2,172 graded call sites.

All five launch languages are now measured:

| language | corpus | L2 def / call | index | confident | overall @ recall | excluded | graded |
|---|---|---|---|---|---|---|---|
| Python | httpx | 99% / 96% | 4.4 s | 99.8% | 70.3% @ 100% | 30.5% | 1,453 |
| Go | go-kit | 100% / 88.3% | 1 s | 88.3% | 59.3% @ 100% | 0% | 1,578 |
| Rust | ripgrep | 99.2% / 99.2% | 40 s | 90.9% | 51.2% @ 100% | 0.15% | 6,642 |
| Java | petclinic | 99.6% / 100% | 361 s | 100% | 90.4% @ 100% | 0% | 324 |
| TypeScript | nest | 99.9% / 83.6% | 3 s | 99.7% | 66.7% @ 100% | 1.94% | 2,172 |

**The shape is two high and two middling**, with Java's 324 sites too few to place. Python
and TypeScript sit near 100% when certain; Go and Rust near 90%. **This ADR does not say
why.** It said why once, from one corpus, and that paragraph had to be retracted three
amendments ago; two rows on each side is a pattern to explain later with a measurement built
for it, not a cause to assert now.

**Two exit-criterion misses, both named rather than averaged.** Java is the only corpus over
the <60 s index budget, at 361 s, because its indexer runs Maven. TypeScript is the only one
below the >=85% call-coverage bar, at 83.6%.

**That 16.4% is two causes, and the first draft of this amendment asserted it was one.** The
join has two ways to decline a call site, and `call_coverage` counts both against itself:
**10.5%** of nest's call sites have no SCIP occurrence at the callee — `scip-typescript`'s
coverage — and **5.9%** have an occurrence and no *enclosing entity*, because a call at
module level has no caller to hang an edge on. `bootstrap()` in a `main.ts` and `describe(…)`
in a spec are calls that no function makes, and nest has hundreds of both.

The second bucket is the criterion's denominator disagreeing with the join by design, not a
coverage gap, and it is not TypeScript-specific: Python 3.6%, Go 0.8%, Rust 0.2%, Java 0%.
Go's own 88.3% is the *other* shape — 10.9% missing occurrences, 0.8% missing callers —
which is why one number could not have told these apart and five can. Writing "this is the
indexer's coverage" without splitting them was, on the same day as two retractions, the
third time a cause was asserted from a number that had not been decomposed.

The 43 exclusions that survive on nest were read rather than attributed: 16 same-file `local`
functions, which carry no name in the symbol to compare against; 7 `typeLiteral268:`
descriptors for methods on anonymous type literals; 1 `<get>` accessor. All descriptor
decoration rather than disagreement, and at 0.36% none justifies a speculative strip of the
kind that would need its own retraction.

`scip-typescript` was not installed on this machine when the roadmap began claiming it as
available, which is the third install hint in two days to be wrong until someone ran it.

## Amendment, 2026-09-06 (seventh) — the import table, in the three languages that had none

Go, Rust and Java recorded no import aliases, asserted as an inverted test since the L1 work
so the gap would be visible rather than rediscovered. The cause was one line:
`_imported_names` dispatched on `style != "python"` straight into the ECMAScript reader,
which finds no ECMAScript import clause inside a `use_declaration` and yields nothing at all.
Go's scope spec even declared `alias_kinds={"import_spec"}` — the intent was written down and
nothing acted on it.

All five launch languages now populate the table. On the pinned corpora: **go-kit 1,131
entries over 223 files, ripgrep 934 over 96, petclinic 469 over 45.**

**"Alias" is a Python and TypeScript name for it.** Java has no import aliases whatsoever,
and its table is still exactly what a resolver needs: `import java.util.List` binds `List`
and says it came from `java.util`. What the table holds is *the local name a file may write,
and where it came from* — 469 of petclinic's 469 entries have a local name that is not the
tail of its source, and so do 889 of ripgrep's 934.

**Go's unaliased name is a convention, not a rule**, and the docstring says so where someone
will read it. The real name comes from the target's `package` clause, which OXN does not
read; the last path segment is what it almost always is. Two documented spellings *are*
rules and are honoured: Go's semantic import versioning, where a trailing `/v2` is a module
major version and never the package name (`github.com/casbin/casbin/v2` is `casbin`, and
go-kit imports six paths of that shape), and gopkg.in's dotted equivalent
(`gopkg.in/yaml.v2` is `yaml`). Where the convention still breaks — `opentracing-go`,
`nats.go`, both in go-kit — the segment is not a valid Go identifier, so the entry is
**inert** rather than wrong: no call qualifier can equal it. A target whose clause disagrees
while still being a valid identifier is mis-bound, and syntax cannot tell.

Four imports deliberately bind nothing, kept as inverted tests in the pattern that worked for
the whole-language gap: Go's `_` (side effects only) and `.` (members enter file scope with
no qualifier at all — a real binding, and a *different* table), Rust's `use s::t::*`, and
Java's `java.util.*`. Recording a plausible-looking name for any of them would be worse than
recording none.

**Every accuracy number is identical to four decimal places**, which is the point rather than
a disappointment: `ProjectSymbols.aliases` is still written and never read. This amendment
records a fact about L0. What it buys is the *next* commit — telling a package-qualified call
(`metrics.NewCounter()`) from a receiver-dispatched one (`c.With()`), which in Go are both
`selector_expression` and are separated only by whether the object is an imported package
name. That is the remaining half of Go's 88.3%, and Rust's ten `use … as …` exclusions are
its acceptance test.

`scopes.py` crossed its 500-line ceiling on this change, so the three readers moved to
`resolve/importers.py`. The seam is real: everything there is a statement about one
language's import syntax, everything left is about scopes and bindings in general. It carries
its own `_text`, as every tree-walking module here does — importing one from `scopes.py`
would make the two modules import each other, and a cycle inside a layer is what
`oxn check --deep` exists to reject.

## Amendment, 2026-09-06 (eighth) — the gate was off for Go, Rust and Java

Measuring the import table's *consumers* found something larger than the table. **Go, Rust
and Java produced zero in-tree dependency edges.** Every import in those languages fell
through `resolve_import`'s ECMAScript branch, resolved to nothing, and was then classified by
`_is_internal_looking` — which asked only "is it relative?" and "is it a JS workspace
package?" — as a third-party dependency. go-kit's 1,131 imports, ripgrep's 1,053 and
petclinic's 471 were therefore all *correctly external*, with **nothing** reported as a
missing edge.

**The consequence is a gate that passes rather than a number that is wrong.** A layered
contract over a Go repository has no edges to judge, so it passes unconditionally. Two
packages importing each other across a declared boundary — a layer violation *and* a cycle —
report `0 violations, passed`. The identical fixture in Python reports the violation, which
is what makes this a language gap rather than a broken test; both are in
`tests/test_go_resolution.py`. Martin's metrics, cycle detection and `ProjectSymbols.imports`
were empty for three of five launch languages for the same reason.

This is the same defect as the monorepo amendment's, three languages wider, and
`docs/metrics.md` section 4.1 is the rule both break: a missing edge must be *visible*.

**Go resolves.** `go.mod`'s `module` line gives the prefix; the remainder is a directory; the
targets are every non-test `.go` file in it, because a Go import names a *package* and a
package is a directory. **0 → 827 in-tree edges on go-kit, and nothing left unresolved.** A
vendored tree needs no special case: `vendor/github.com/other/dep` declares a different
module path and stays external, which is what it is. A nested `go.mod` declares a separate
module and is deliberately not read.

**Rust and Java are made visible and not resolved**, with the reason named rather than left
as "later". Rust: `crate::`/`super::`/`self::` are always ours, and so is any first segment
naming a Cargo workspace member — read from `[workspace] members` and each member's
`[package] name`, **with hyphens swapped for underscores**, because a crate declared
`grep-searcher` is imported as `grep_searcher` and keying on the declared name matches
nothing an import ever writes. That took ripgrep's reported-missing from 17 to **611**: the
first attempt split on `::`, and `extract_imports` normalises Rust paths to dots, so only a
bare `super` matched. Java: an import is ours when its package prefix is one the tree's
layout declares. Resolution additionally needs `mod` walking across `x.rs`, `x/mod.rs` and
`#[path]` plus re-exports (Rust), and source-root discovery across Maven layouts and Gradle
source sets (Java). Neither has a pinned measurement, and wiring them unverified is the
mistake this ADR's monorepo amendment was written about.

**What it did to the numbers.** Only Go moved, and it moved because `resolve_call`'s second
step — "a declaration in a file this one imports" — had never fired for it: **confident recall
55.5% → 78.0%, overall precision 59.3% → 72.9%, confident precision 88.3% → 87.4%.** Correct
confident answers 773 → 1,075; wrong 102 → 155. All 155 are `selector_expression`, splitting
102 same-file (the `With` collisions) and **53 of a new shape — `c.Value()` on a local of an
imported type, answered with the imported package's `Value`**. Those 53 are exactly what the
import table of the previous amendment exists to stop, and stopping them needs `resolve_call`
to see the qualifier. Python, Rust, Java and TypeScript are identical to four decimal places.

`resolve.py` crossed its 500-line ceiling, so every manifest reader moved to
`graph/manifests.py` — `tsconfig.json`, `package.json`, `go.mod`, `Cargo.toml`, and Java's
layout, which is the manifest Java does not have. `mypy` then caught what CI's 3.10 job would
have: `tomllib` is 3.11, and OXN supports 3.10. The replacement is a section-aware TOML field
reader, which is the part that actually matters — `crates/matcher/Cargo.toml` declares `name`
twice, under `[package]` and under a test target two lines later.

## Amendment, 2026-09-06 (ninth) — `resolve_call` learns what a call was written through

`ProjectSymbols.aliases` was written and never read from the day it existed. It is read now,
and the two rules that read it were both derived from splits rather than assumed.

**The first split: confident precision by qualifier kind.** A call is *bare* (`helper()`),
*import-qualified* (`metrics.NewCounter()`) or *receiver-qualified* (`c.With()`).

| language | bare | import-qualified | receiver |
|---|---|---|---|
| Python | 100% | 100% | 99.3% |
| TypeScript | 100% | 100% | 99.7% |
| Java | 100% | — | 100% |
| Go | 100% | 88.5% | 75.8% |
| Rust | 99.3% | — | 80.8% |

That table kills the obvious policy. "Never be certain about a receiver call" would have
cost TypeScript four fifths of its confident answers and Java seven eighths, to fix nothing:
their receiver calls are already right.

**The second split found the mechanism.** Splitting the same confident answers by *which
resolution step* produced them: step 1 is the calling file's own top-level declarations.

| language | step 1 | later steps |
|---|---|---|
| Rust receiver | **0 / 265** | 1,114 / 1,114 |
| Go import-qualified | **0 / 51** | 415 / 418 |
| Python receiver | **0 / 2** | 283 / 283 |
| Go receiver | 91 / 142 | 225 / 275 |
| TypeScript, Java | never fires | 99.7% / 100% |

**Every confidently-wrong qualified answer on every corpus came from step 1.** So the rule is
not about receivers versus packages at all: *a qualified call is never answered by the
calling file's own top-level declarations*. They say nothing about a receiver's type, and
nothing about another package's contents.

    language      confident precision      confident recall
    Rust          90.9% -> 99.6%           45.9% -> 42.0%
    Go            87.4% -> 93.0%           78.0% -> 74.6%
    Python        99.8% -> 100%            65.6% -> 65.5%
    Java, TS      unchanged                unchanged

Recall stays at 100% everywhere: nothing stops being answered, some answers stop being
*certain*, and only certain answers may gate.

**This also answers a question two amendments ago left open** — why Python and TypeScript sat
near 100% while Go and Rust sat near 90 — and answers it from a measurement rather than a
shape. A Python, TypeScript or Java method lives inside a class, so it is not a *top-level*
declaration and never entered the table step 1 reads; Go methods are top-level, and Rust's
`impl` methods reach it through the same path. Those languages were not better resolved, they
were *not exposed to the defect*. The earlier retraction was of a story told without this
split; this is the split.

**The second rule reads the table's contents rather than its keys.** A call through an import
that reached no file in this tree resolves to nothing: if `np` names `numpy` and `numpy` is
not here, `np.array()` is not an entity here, and any in-tree `array` is a coincidence. The
grader is blind to this class — the oracle places these callees out of tree, so they are
never graded — but they reach `metrics.callgraph`, CBO and RFC, where L1 answered **233
confidently on go-kit and 16 on httpx**. Both are 0 now, verified by re-running the probe
rather than by assuming.

Only a *placed* specifier counts. An absent entry is not evidence of externality, and it is
the common case in Rust and Java, where `use std::fmt` records `fmt -> std` while the import
specifier is `std.fmt`: the two never join, and the rule correctly declines to fire rather
than declaring most of two languages external.

**And it stops at a call in the chain.** `verify(repo).findByLastName()` is Mockito: `verify`
is an external static import, but the mock it returns has an in-tree type and the method
really is ours. Asking the import table about `verify` declared that call external and cost
exactly two graded answers on petclinic — found by checking what the 229 firing sites were
rather than accepting a recall of 99.4%. A chain headed by a call is qualified by a *value*,
not by a module; the locality rule still applies to it, the module rule does not.

The product changed with the grader, deliberately. `metrics/coupling.py` passes the receiver
it already collected, because every name in `MethodAccess.calls` is by construction a call
through the receiver — measuring a fix the product does not have is the failure this ADR has
recorded twice today.

Still not read: *which* module an import-qualified call names. Restricting `metrics.Foo()` to
the files `metrics` resolves to would gain at most 3 answers on go-kit — later steps already
score 99.3% there — so it is not built, and this is what that decision was based on.
