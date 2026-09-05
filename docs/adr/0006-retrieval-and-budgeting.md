---
id: ADR-0006
title: Retrieval and the constraint bundle — BM25 over decisions, budgeted, never blocking
status: accepted
date: 2026-09-04
tags: [retrieval, bm25, constraint-budgeting, adr, context, mcp]
applies-to: ["src/oxn/context/**", "src/oxn/rules/adr.py"]
---

# ADR-0006: Retrieval and the constraint bundle

## Status

Accepted — 2026-09-04. Implements P8. Consumes the rules [ADR-0005](0005-rule-engine.md) produced
and is bounded by [ADR-0003](0003-enforcement-model.md)'s two channels; neither is changed. The MCP
server that will serve the bundle is P9 and is explicitly not decided here.

## Context

P7 made every invariant *data*: eleven built-in rules, plus whatever `oxn.yaml` and the ADRs add.
That success creates the problem this ADR answers. There are now more constraints than an agent
should be shown, and three 2026 results say showing them all is not neutral but actively harmful:

- **Constraint decay** (arXiv 2605.06445): assertion pass rate drops ~30 pp from unconstrained to
  fully constrained. More context is not more compliance.
- **Architecture as Capability Equalizer** (arXiv 2608.21747): *format* moves the result more than
  content does, and inversely with model strength.
- **AI-Generated Smells** (arXiv 2605.02741): prompt specificity had zero effect (p>0.8) on
  architectural decay; code volume correlated at ρ=0.94.

So the question is not "how do we surface the constraints" but **which subset, ranked how, in what
shape** — and how we would ever know the ranking is any good.

That last clause is where the ROADMAP's exit criterion has a hole worth naming before any code is
written. It asks for "retrieval precision@3 against hand-labelled task → ADR pairs". OXN's own
corpus is **six** decisions. Three picks out of six is half the corpus; a ranker that returns the
three longest documents scores respectably. And a label written by the person who wrote the ranker,
by reading an ADR title and paraphrasing it into a task, measures paraphrase rather than retrieval —
the same circularity this project already refused in P10's threshold fitting. Section 5 fixes both,
and the ROADMAP is amended to match.

## Decision

### 1. Retrieval never blocks — and the gate never imports it

The gate reads *rules*; the bundle reads *rankings*. BM25 has no path into `run_check`, no path into
`oxn check --json`, and no path into the hook. A score that can be wrong by degrees must never
decide a boolean, and ADR-0002's rule — only an `EXACT`, fresh measurement may block — would be
quietly re-opened by any ranking that could suppress a finding.

This is an invariant rather than an intention, so it is testable in the same way `test_import_guard`
already pins the fast path: **`oxn.check` and `oxn.rules` may not import `oxn.context`.** The
dependency runs one way only. A finding is never produced, ranked, hidden or reordered by anything
in this ADR; the bundle is a *second* artifact, computed for the context channel, out of the same
rule objects.

The layered contract in `oxn.yaml` is extended to say so structurally rather than only in prose.
`src/oxn/rules/*` has been in **no layer at all** since P7 — the layered check skips edges whose
source is unassigned, so every import the rule engine makes has been ungoverned. It joins `analysis`
alongside the new `src/oxn/context/*` in the same change.

### 2. One ADR parser. `load_decisions` grows two fields and no second reader

`Decision` gains `tags: tuple[str, ...]` and `body: str`. `_frontmatter` already reads the whole
file and throws the body away; keeping it costs nothing. `load_decisions` keeps its signature, so
`check.py` and the P7 tests do not move.

**A second frontmatter parser living in the retrieval module is the failure mode**, not the
duplication itself: two parsers disagree about a malformed header, and then the document the gate
enforces is not the document the agent was shown. There is one reader.

What may *not* happen in `adr.py` is indexing. It is on the hook path — `check.py` calls
`load_decisions` on every edit — and a BM25 index over a whole `docs/adr` tree is report-path work.
The index is built lazily in `oxn.context`, by callers the hook never reaches.

### 3. BM25 is Okapi, self-implemented, stdlib only

`math`, `re`, `collections`; no dependency, consistent with ADR-0001 and with the ROADMAP's estimate
of ~60 lines. `k1 = 1.5` and `b = 0.75` are the standard defaults and enter `thresholds.py` with the
same honesty as the other nine tunables: `Evidence.LITERATURE`, zero observations, fitted when
section 5's labels exist. A parameter that hides in a scoring function is the folklore
`thresholds.py` exists to prevent.

The tokenizer splits on non-alphanumerics **and** on `_` and camelCase boundaries. Task descriptions
and ADR bodies both name identifiers — `load_decisions`, `DependencyGraph` — and a tokenizer that
treats those as opaque single terms throws away the most discriminative words in the corpus.

Ties break on the decision identifier, so the ranking is total and reproducible. A ranking that
varies with dictionary order cannot be regression-tested, and section 5 depends on it being.

**Why not embeddings first.** `model2vec` is a static-embedding model, which means a model *file*,
which means a network fetch — permitted by ADR-0001 only as an explicit install or index step, never
at query time, behind a lazy import in the `oxn[semantic]` extra. It is also not worth building
before section 5 exists: a second ranker earns its place by being *compared*, on the same labels, to
the first one. Built first, it is an unfalsifiable improvement.

### 4. What is ranked is a constraint, not a document

The unit in the bundle is a rule with its provenance, not an ADR. "Here are three relevant
documents, go read them" is prose guidance; the capability-equalizer result is that
machine-parseable contracts are what move a weak model from 33% to 100% route coverage.

Two signals, combined rather than blended into one opaque number:

- **Relevance** — BM25 of the task text against the constraint's provenance text: the ADR's title,
  tags and body for an ADR-derived rule; the rule and metric name for a built-in or `oxn.yaml` one.
- **Blast radius** — how much of the task's target scope the rule actually governs, from the same
  `applies-to` and layer relations the gate joins on.

**Blast radius dominates a tie.** A ceiling that governs the file being edited is relevant whether
or not the task description happens to use its vocabulary, and the failure of a pure-text ranker on
a two-line task ("fix the parser") is exactly that it cannot see this. Retrieval ranks what scope
cannot decide.

*Added 2026-09-04, from the measurement below.* When the caller gives **no** target files —
`get_architectural_context("add a retry to the HTTP client")` with nothing else — there is no scope
to partition by, and section 5 shows text alone performs near chance on a single-project corpus. So
that path needs a third input: a **breadth prior**, the amount of code each constraint governs.
It is the same relation the blast radius reads, marginalized over the repository instead of over a
task's files, and on OXN's own labels it beats the text ranker outright. Naming it as a prior rather
than letting it hide inside the text score keeps it visible in the emitted bundle, where "this
governs most of the tree" is a fact the agent should see rather than a thumb on a scale it cannot.

The same number therefore reads in both directions, and the implementation says so: with target
files, scope has already selected the covering constraints and the *narrower* of two comes first --
a ceiling governing every file is ambient, one scoped to the area being edited exists because of it.
Without target files, there is nothing to be specific about and the order inverts into the prior.

The bundle is capped by count, from `thresholds.py`, and emitted as a typed structure — pydantic,
already a dependency, so the JSON Schema §3.2 asks for is free. Never prose paragraphs.

*Added 2026-09-04, and it changed the design.* Ranking rules and decisions in **one** list does not
work, because BM25 scores are comparable between documents of a similar shape and a six-token
ceiling statement is not the same shape as a 17,000-character ADR. Measured across the 53 labelled
tasks: **159 of 159 top-three slots were prose**, and ADR-0001 — the longest general document,
scoped `**`, excluded from the labels precisely because it discriminates nothing — took a top-three
slot on 14 of them. The bundle had become "here are three documents to go read", which is the thing
this section opens by rejecting, and §5a's longest-document baseline is the same failure under
another name.

So **the cap is shared: half to constraints the gate enforces, half to decisions it cannot check**,
each side ordered by the key above and the shorter side yielding its slots to the other. Gated rules
are emitted first, because they are what will actually reject the edit. After the split, prose takes
0 of 159 top-three slots and the layered contract leads a question about layering.

One ordering choice is worth naming rather than leaving to be discovered: relevance still sits
*above* specificity and breadth in the key, even though §5a measured text at near-chance. Within a
stratum that is right — the documents are then of one shape, and matching "raise the file length
limit" to `file_sloc` is exactly what text is for. It was the cross-stratum comparison that was
invalid, not the ranking.

### 5. How we would know it works

Precision@3 stands as the headline number, with two corrections.

**(a) Six documents cannot support @3.** On OXN's own corpus the reported statistics are **P@1 and
MRR**; `precision@3` is measured on an external corpus of at least twenty decisions, pinned in
`benchmarks/manifest.yaml` and `lock.json` exactly as `python-httpx` and `typescript-nest` already
are. A retrieval number on a corpus small enough that random guessing scores well is not evidence.

**(b) Labels the ranker's author wrote are not labels.** Two independent sources, and neither is a
paraphrase of an ADR title:

- **Git supplies OXN's own labels for free.** `git log --name-only` gives (commit message, changed
  files); `applies-to` gives the decisions covering those files. The pair *(what somebody actually
  set out to do, which decisions in fact governed the code they touched)* is a task → ADR label
  nobody hand-wrote and nobody could have tuned. It is noisy — a commit touching a widely-scoped
  file inherits its ADR — and it is honest, which is the trade this project keeps making.
- **The external corpus is labelled from the task side**, written as a developer would state the
  work ("add a retry to the HTTP client"), never by reading a decision and reversing it, and
  confirmed before any ranker is run against it.

Both land as **tests asserting a floor**, not scripts that print a number. A metric that lives in a
script drifts silently; one that lives in a test fails the build the day it regresses.

### 5a. What the git labels actually measured — a negative result, recorded the day it appeared

*Added 2026-09-04, before the bundle was built, which is the point of having measured first.*

The labels extract cleanly: 93 commits, 53 pairs, five discriminating decisions. ADR-0001 is
excluded because its scope is `**` — a decision that governs everything is correct for every commit
and discriminates nothing, and a label set with an always-right answer measures nothing. Documents
are title, tags and prose; `applies-to` is **not** in the ranked text, because the labels are made
of it and a ranker reading its own gold would report a number about nothing.

| ranker | P@1 | MRR |
|---|---|---|
| BM25 over decision text | 0.377 | 0.579 |
| uniform random | 0.362 | — |
| constant, ordered by how much code each decision governs | 0.698 | 0.787 |
| constant, always the longest decision | 0.245 | — |

**BM25 over ADR prose performs at chance here, and a constant beats it.** Four things that number
is, and one it is not:

1. **It is largely a statement about the labels.** The gold is *scope* and the ranker has no scope,
   so the task being scored is "can a commit subject predict which decisions' globs the changed
   files fall under". `refactor: halstead 15 -> under` cannot, and should not be expected to.
2. **The winning baseline is that same signal wearing a hat.** The constant order is
   `[0002, 0004, 0003, 0005, 0006]`, which is the decisions sorted by how much of the tree they
   govern; it is estimated on the labels it is scored against, so it is not independent evidence.
   It is still a baseline any ranker must beat, and this one does not.
3. **The corpus is a single project, so its vocabulary is shared.** The misses are not
   unanswerable: 32 of 33 share content words with a gold decision, a median of three. Every ADR
   here discusses architecture, latency and the graph, so the words that match match everywhere.
   This is the specific way a six-document, one-project corpus fails, and it is why section 5(a)
   requires an external corpus rather than merely preferring one.
4. **It is not a ranker bug.** Dropping ~20 stopwords moves P@1 by 0.000 and MRR by 0.001 —
   expected, since with five documents the IDF ratio between a rare term and a universal one is
   about 16:1, so common words never decide a rank. No stopword list was added on the strength of a
   diagnostic that said it would do nothing.

What it is **not** is a verdict on BM25 or on retrieval. It is a verdict on this corpus, and it
converts section 4's argument into evidence: scope decides, text orders within. The external corpus
moves from planned to load-bearing — no positive claim about text retrieval is available without
it — and the quality floor lives there, not here.

**The labels are frozen, and regenerating them is changing the benchmark rather than
maintaining it.** Measured the day after P8 landed: regenerating would have added three pairs —
three commits *about the retrieval system* — which score P@1 1.000 between them and lift the
headline to 0.411. They score perfectly because their subjects and this document were written by
the same author within hours, which is section 5's paraphrase circularity arriving through the back
door. `scripts/extract_retrieval_labels.py --status` therefore reports "stale" from the first commit
after an extraction, deliberately — and is named `--status` rather than `--check`, exiting 0, because
every `--check` a developer has met is a CI gate and a flag name argues more effectively than a
paragraph asking not to be wired into one.

One reflexivity note, since it caught us while writing this section: **the corpus contains this
document**, so recording the numbers changed the text being measured. The recorded values are taken
after this amendment, and any edit to any ADR moves them. That is deliberate; the test carries the
delta the way `.oxn/baseline.json` carries the gate's — and the live value is the one pinned in
`tests/test_retrieval_quality.py`, not the one in this table, since the table ages the moment
anything here is edited. Writing the number into the table above does not move it further: swapping
one rare token for another changes no document's length and no other term's frequency, so it is a
fixed point rather than a regress. Amending the section *around* it does move it — 0.584 to 0.579
as this section grew, which is the test doing its job rather than a defect.

### 5b. The external corpus, and the number it produced

*Added 2026-09-05.* Section 5(a) made this corpus load-bearing rather than planned. Finding it
produced a second negative result before it produced a positive one, and the negative one is the
more interesting of the two.

**Teams cite decisions when writing them, not when obeying them.** The funnel: 100 repositories
with an `adr-tools` tree, 18 with twenty or more numbered decisions, 15 cloned and measured. Of
those, the citing commits are overwhelmingly `Add ADR-25`, `Reword ADR-25`, `Mark ADR-25 as
Accepted` — the subject *is* the decision's title, so the pair is a paraphrase with the paraphrase
left in. Commits citing a decision while doing the work it governs are rare: three repositories
yielded fewer than ten each, one yielded none, and one yielded 76.

That is worth stating as a finding rather than an obstacle, because it is the strongest argument
this project has for its own convention: **OXN's decisions are evaluable because their scope is
declared.** `applies-to` is what makes "which decisions govern this change" answerable without
anyone remembering to mention an ADR in a commit message. No third-party corpus has it, which is
why the label here had to be a citation instead.

**The corpus.** Five repositories, pinned in `benchmarks/manifest.yaml` with `history: full`
(a label here *is* a commit, so a shallow clone contains none of them): `querator`, `Darker`,
`elsa-core`, `tessellation`, `agentic-dev-team` — 149 decisions and 100 pairs. Each pair is ranked
**only against its own repository's decisions**, which is what `get_architectural_context` does;
pooling is for the statistics and never for the ranking.

| slice | n | BM25 P@1 | **P@3** | MRR | citation-prior P@3 |
|---|---|---|---|---|---|
| all | 100 | 0.610 | **0.820** | 0.726 | 0.470 |
| human-authored commits | 48 | 0.604 | **0.833** | 0.722 | 0.479 |
| task text does not restate the title | 44 | 0.545 | **0.773** | 0.680 | 0.318 |
| strict — both of the above | 27 | 0.481 | **0.741** | 0.632 | 0.259 |
| excluding the largest repository | 24 | 0.583 | **0.875** | 0.723 | 0.396 |

**BM25 beats the citation-frequency prior in every pooled slice**, including the strict one. That
is the claim section 5(a) could not make, and it is asserted rather than reported:
`tests/test_retrieval_external.py` fails if it stops holding.

*Pooled* is the honest unit, and the qualifier is not a hedge. Per repository the two smallest
corpora tie the prior — `tessellation` 1.000 against 1.000 on four pairs, `elsa-core` 0.800 against
0.800 on five — which at that size is not a result in either direction. The pooled slices are what
the assertion is made on, and the per-repository numbers are reported so the tie is visible rather
than averaged away.

**This does not overturn 5(a); it explains it.** The two corpora ask different questions. There the
gold is *scope* — which decisions' globs the changed files fall under — and a commit subject cannot
recover that, which is why text scored at chance. Here the gold is *topic* — which decision this
work is about — which is exactly what words are for. Read together they are section 4's split with
evidence on both sides: **scope decides which constraints apply; text decides which of them this
task is about.** A ranker that had been asked to do the first job was always going to look useless.

**Half of every usable pair in open source is agent-written.** 52 of 100 carry
`Co-Authored-By: Claude`. They are kept, and reported separately, on the grounds that OXN exists to
govern agents — so agent-written commits are arguably the population rather than the confound. The
`human-authored` and `excluding the largest repository` rows exist so a reader who disagrees can
have the number their way; the conclusion does not change in either.

**What is still not measured.** Whether a *better* ranker helps: `model2vec` is deferred, and the
comparison it needs — a second ranker on these same labels — is now possible for the first time.
And whether any of this helps an *agent*, which is P11 and needs the arms.

## Consequences

**The bundle is P9's payload, and P9 is not this ADR.** `get_architectural_context` is where this
gets served. Building any of the MCP server here would couple the ranking to a transport before the
ranking is measured.

**The context package is off the fast path, and the guard test says so.** `oxn.cli`'s import graph
must not reach `oxn.context`; a human `oxn context "<task>"` subcommand lives behind typer with
every other pleasant, expensive thing.

**This ADR does not test the hypothesis.** Whether budgeting *mitigates* constraint decay needs the
arms — `{no OXN, CLAUDE.md-only, MCP-only, hooks-only, hybrid, hybrid + budgeting}` — and that is
P11. P8 delivers the mechanism and the retrieval quality number; it must not be reported as if it
were the effect on agents. The null result stays publishable, and stays P11's to find.

**A ranking cap is a policy, and policies get gamed.** Capping at *n* constraints means the n+1-th
is not shown — and if the cap is what an agent violates, the ranking became load-bearing after all.
That is why section 1 is absolute: the gate still enforces every constraint, shown or not. The
bundle changes what the agent *knows*, never what the gate *accepts*.
