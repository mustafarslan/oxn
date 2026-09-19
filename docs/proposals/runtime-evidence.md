# Proposal: runtime evidence

**Not an ADR, and deliberately not in `docs/adr/`.** `test_oxns_own_decisions_parse` requires every
shipped decision to be `accepted`, because an accepted ADR with an `applies-to` scope becomes a
constraint the rule engine serves to agents. This is a proposal awaiting a decision and a
measurement, and its scope would have governed `src/oxn/volume/**` — live duplication code — on
behalf of a feature that does not exist. Weakening that invariant to admit this document would be
the thing the invariant is for.

Becomes `docs/adr/0007-runtime-evidence.md` with `status: accepted`, an `applies-to` naming files
that exist, and the resolution rate below filled in — or it is deleted. There is no third outcome
where it sits here indefinitely.

## Status

Proposed — 2026-09-18. Extends the hotspot ranking [docs/metrics.md §5.4](../metrics.md) already
defines and is bounded by [ADR-0001](../adr/0001-dependency-policy.md)'s dependency rule,
[ADR-0002](../adr/0002-resolution-strategy.md)'s hook budget and [ADR-0004](../adr/0004-freshness-model.md)'s
staleness model. None of them is changed. Nothing here gates, so
[ADR-0003](../adr/0003-enforcement-model.md)'s two channels are untouched.

## Context

OXN is entirely static. Every metric it computes is a function of source text and, in Tier 2.5, of
the history of that text. Its hotspot ranking is Tornhill's product of two normalised factors,
change frequency and cognitive complexity, and §5.4 already says what it is for: *"prioritisation
only. A hotspot never blocks a commit."*

Both factors are proxies for the same unanswerable question. Change frequency asks which code people
keep having to touch; complexity asks which code is hard to touch. Neither can say which code
*costs* anything when it runs, and a gatekeeper that ranks what to fix while having no access to
that is ranking on two thirds of the evidence.

The occasion for deciding this is a plan for two agent skills — one that profiles a program across
six languages and proposes fixes, one that load-tests a service. That plan is not this ADR's
subject and nothing in it is adopted here. What it exposes is a seam. Its profiling step ends by
mapping profiler symbols to source locations with `grep`, and calls that step lossy in its own text.
Resolving a symbol to an entity, a file and a line, per language, from a normalised graph is the one
thing OXN already does properly and a skill cannot do at all.

## Decision

**OXN consumes runtime profiles. It never produces them.**

The input contract is folded stacks, `frame;frame;leaf <count>`, one path per line — **and every
frame must carry `file:line` where its producer knows it.** Not pprof, not JFR, not `.cpuprofile`:
those are a protobuf parser, a JDK tool invocation and a V8 implementation detail respectively, and
each is either a dependency ADR-0001 refuses or a shell-out to a toolchain that may not exist.
Whatever produced the profile owns the conversion; OXN reads text.

**The `file:line` half of that contract was added after measuring, and the first draft of this
document was wrong without it.** Symbol names alone do not join. Measured on go-kit's `sd`
benchmark, 3,819 samples, 79 distinct frames of which 6 are in project code: joining on symbol name
resolved **4 of 6**, joining on the file and line the profile already carries resolved **6 of 6**.

The two misses are one failure and it is systematic rather than unlucky. Go names an anonymous
function `BenchmarkEndpoints.func2`; OXN names the same function `<func_literal@26>`. Neither
spelling is wrong and no normalisation reconciles them, because the number in each is counting a
different thing — Go counts closures within the enclosing function, OXN records the line. Python
lambdas and JavaScript arrow functions are the same shape, and closures are not a rare inhabitant
of hot paths: callbacks, handlers and deferred work are most of what an event-driven program does.

So a symbol-only profile is accepted and its resolution rate is reported, but it is the degraded
mode, and the report must say which mode it ran in. Every producer worth reading already carries
the richer form: `go tool pprof -raw` prints file and line per frame, py-spy's raw format writes
`func (file.py:42)`, and async-profiler can be asked for line numbers.

This is not a compromise reached for want of effort. A gatekeeper that runs a profiler acquires the
right to install one, to ask for `sudo`, to need `perf_event_open`, and to behave differently on
Darwin than on Linux — and the rule that has kept OXN to two runtime dependencies is the same rule
that says no here.

**Runtime weight ranks; it never gates.** It does not appear in `oxn check`, cannot produce a
finding, cannot enter `.oxn/baseline.json`, and no ceiling is expressed in samples. It is a third
factor in the §5.4 product and is surfaced exactly where the other two already are:
`oxn metrics --sort-by hotspot`, with all axes visible so a reader sees why a thing ranked, and the
MCP `get_architectural_context`, where "this function is 41% of the samples in the profile you gave
me" is the kind of thing an agent should know before editing it and the kind of thing OXN must never
turn into a refusal. CLAUDE.md's "a violation is not a suggestion" holds precisely because this
produces no violations.

**Profiling never touches the hook path.** ADR-0002's budget is unchanged because nothing here runs
during a check. This matches clone detection, which is already report-path only for the same reason.

**Unresolved symbols are counted and reported, never dropped.** A profile is stamped at whatever the
program was when it ran; the graph is whatever the source is now. Symbols that do not resolve are
the *expected* case under a rename, not an anomaly, and the report says "N distinct leaf frames, M
resolved, K unmatched" in the shape `incomplete_paths` and `ungoverned_files` already use. A
resolution rate is itself the measurement of whether this feature works, and a silent drop would
turn a broken join into a short list of confident findings.

**Symbol normalisation belongs to the language profile.** `pkg.(*T).M`, `func (file.py:42)`,
`fn file:line:col` and `com.x.C.m` are four spellings of "which entity is this", and the runtime and
bootstrap frame prefixes that have to be collapsed — `runtime.`, `_PyEval`, `node:internal/` — are
per-language facts too. They go on `LanguageProfile` beside the other per-grammar knowledge, not
into a parser that switches on a string.

## Consequences

**Positive.** The hotspot product gains the factor it was always missing, with no new dependency and
no new gate. The join is the part nobody else can do cheaply: a profiler knows symbols and counts, a
`grep` knows text, and OXN knows entities.

**Negative.** A folded-only contract puts a conversion step on whoever brings the profile, and that
step is where a naive caller will lose the mapping between a symbol and a source file. The
resolution rate is the honest report of that cost and will not always be high.

**What this ADR deliberately does not decide.** Comparing two profiles. The difference between
runtime shares before and after a change is a real question with a subtle answer — the margin for a
difference of proportions is `z·√(p₁(1−p₁)/n₁ + p₂(1−p₂)/n₂)`, not the sum of two one-sample
margins, and a fix that removes work *raises* the share of every frame it did not touch, so shares
compared naively report improvements as regressions. OXN reports no before/after today. When it
does, that is its own decision and it needs the calibration treatment `oxn calibration` gives every
other number, not an assumption.

Load testing is out of scope and stays out. Driving traffic at a service is an action with a blast
radius, and OXN takes no actions.

## Verification

**First measurement, 2026-09-18, Go.** `go test -bench=. -benchtime=3s -cpuprofile` on go-kit's
`sd` package — the corpus's only benchmark — then `go tool pprof -raw`, against
`oxn metrics --json` over the same tree (1,938 entities, 218 files).

| | |
|---|---|
| samples | 3,819 |
| distinct frames, all stacks | 79 |
| in project code | 6 (8%) |
| runtime and stdlib | 73 (92%) |
| resolved by symbol name | 4 / 6 |
| resolved by file and line | 6 / 6 |

Three things follow, and only the third is a judgement.

**The contract needed `file:line`,** as recorded above. This is the measurement that found it.

**A digest must collapse runtime frames, and 92% is the reason.** Not a token-saving nicety: a
ranking that lists `runtime.findRunnable` and `sync.(*RWMutex).RLock` above the two functions the
program is actually made of is not a worse ranking, it is a different one about the Go scheduler.

**The sample is too small to decide the feature and is not presented as deciding it.** Six project
frames from one microbenchmark is an existence proof that the join works and a demonstration of one
systematic failure in it. It is not a resolution rate for Go, let alone for anything else. What it
justifies is a second measurement on a profile with real application code in it — the httpx bed
under load would do, since `load-test`'s own fixture is a slow HTTP service — and what it does not
justify is writing the feature.

**Second measurement, 2026-09-18, Python, and this one is large enough to decide.** The first
left six project frames from one Go microbenchmark, which proved the join runs and proved
nothing about how often it lands. So: `cProfile` over OXN running `oxn metrics` on the httpx
corpus, joined against `oxn metrics --json` over OXN's own source (958 entities, 72 files).

| | |
|---|---|
| distinct frames | 1,434 |
| in OXN's own source | 149 |
| stdlib and dependencies | 1,285 (90%) |
| of the 149, real named functions | 92 |
| of the 149, not source functions at all | 57 |
| **resolved by symbol name** | **44 / 92 (47.8%)** |
| **resolved by file and line** | **85 / 92 (92.4%)** |

**The contract's `file:line` requirement is now settled rather than argued.** Symbol names
resolve under half of real Python frames against 92% for file and line -- a two-fold gap where
Go showed 4 of 6 against 6 of 6, which was suggestive and too small to rest on. The Python
failures are also different in kind from Go's closures: `<module>`, `__annotate__`,
`<listcomp>`, `<genexpr>` and dataclass-generated `__init__` frames carry names no source
function has, and 57 of the 149 are of that sort. A symbol-keyed join does not merely miss
them, it has nothing to miss them *with*.

**90% of frames are not the program's own code**, against Go's 92%, in a different language
with a different runtime. Collapsing them is a property of profiling rather than of a
particular toolchain, which is the strongest available argument for the digest step.

The seven real functions left unresolved by file and line are all dataclass `__init__`s that
`cProfile` labels with the class name, so the honest reading of 92.4% is that it understates:
the join misses almost nothing it should catch, and what it misses is not source.

Below some rate this would be a worse `grep`. **At 92.4% it is not**, and the remaining work
is the feature rather than another measurement to justify it.
