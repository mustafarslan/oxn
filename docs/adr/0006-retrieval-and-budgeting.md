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

The bundle is capped by count, from `thresholds.py`, and emitted as a typed structure — pydantic,
already a dependency, so the JSON Schema §3.2 asks for is free. Never prose paragraphs.

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
