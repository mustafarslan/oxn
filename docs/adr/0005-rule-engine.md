---
id: ADR-0005
title: Rule engine — relations and conjunctive rules, hand-evaluated, Datalog-shaped
status: accepted
date: 2026-08-31
tags: [rules, datalog, contracts, adr-constraints, architecture]
applies-to: ["src/oxn/rules/**", "src/oxn/check.py", "src/oxn/config.py"]
---

# ADR-0005: Rule engine

## Status

Accepted — 2026-08-31. Implements P7. Extends [ADR-0002](0002-resolution-strategy.md)'s gate
policy and [ADR-0003](0003-enforcement-model.md)'s enforcement split; neither is changed.

## Context

Every check OXN performs today is hand-coded Python. Six ceilings in `_ceiling_findings`, four
contract kinds in `contracts.py`, one anti-gaming rule in `metrics/shredding.py`. They work, and
the gate reaches zero on OXN's own tree — but they are *code*, and P7's exit criterion is that a
rule set expressed purely in `oxn.yaml` and ADRs reproduces all of them.

That criterion is not a refactor for its own sake. It is what makes two claims true:

- **ADRs as a constraint source is OXN's distinctive idea** versus Mneme (no metric ceilings) and
  Sonar (no ADRs). It is only a contribution if the constraints are genuinely *data* — an ADR that
  compiles to a hand-written Python branch has contributed nothing a config file could not.
- **A project must be able to state an invariant OXN's authors never anticipated.** Today that
  means editing `check.py`.

The ROADMAP adds a constraint that shapes everything below: start with a hand-rolled evaluator, but
design the rule surface so an in-house semi-naive Datalog engine can back it later **without
changing rule syntax**. That is easy to violate on day one and expensive to repair afterwards, so
this ADR fixes the surface before any evaluator exists.

## Decision

### 1. Facts are relations, projected from the store — never re-derived

The fact base is the extensional database. Each relation is a named, typed tuple set projected from
what the graph store already holds. Nothing here parses, walks a CST, or computes a metric:

| relation | tuple | source |
|---|---|---|
| `entity` | (id, path, kind, qualified_name, line) | `store.entities_for` |
| `metric` | (entity_id, key, value, exactness, bound) | `store.measurements_for` |
| `layer` | (path, layer) | `contracts.assign_layers` |
| `imports` | (source_path, target_path, kind) | `depgraph.DependencyGraph` |
| `contains` | (parent_id, child_id) | `Entity.parent_id` |
| `cycle` | (component, member) | `algos.strongly_connected_components` |

**A rule that needs a fact the store does not hold is a finding to record, not a tree walk to
write.** The relation list is the API a future Datalog engine loads; growing it casually is how the
hook path gets slow.

**`oxn.yaml` compiles to facts as well as rules.** A layered contract's ordering and a
`deep_import`'s entry points are data the rules join against, so they enter the same EDB at load
time rather than living as evaluator special cases:

| relation | tuple | source |
|---|---|---|
| `ceiling` | (metric_key, path, value) | `Config.ceiling_for`, per layer |
| `allowed_edge` | (contract, source_layer, target_layer) | `layered` contract order |
| `forbidden_edge` | (contract, source_layer, target_layer) | `forbidden` contract |
| `independent` | (contract, module) | `independence` contract |
| `entrypoint` | (contract, path_glob) | `deep_import` contract |
| `callable` | (kind) | the three callable entity kinds |
| `file_kind` | (kind) | file and module |
| `runtime` | (import_kind) | `RUNTIME_KINDS`; a type-only import is not coupling |
| `blocking` | (exactness, bound, may_block) | ADR-0002's policy, projected from `MetricValue` |
| `explanation` | (entity_id, metric_key, trail) | the increment trail, always present |
| `edge_label` | (source, target, "source -> target") | the string a contract finding is keyed by |
| `entrypoint_match` | (contract, path) | globs resolved against known paths |

That is the **entire** built-in surface: twelve extensional relations and no functions. There are no
"built-in predicates" beyond arithmetic and string comparison on bound variables. A migration story
is only as credible as this list is short and written down.

### 2. A rule is a conjunctive body over relations, plus stratified negation

```yaml
rules:
  - name: cognitive_complexity
    when:
      - entity(E, Path, Kind, Name, Line)
      - metric(E, "cognitive_complexity", V, Ex, B)
      - blocking(Ex, B)
      - callable(Kind)
      - ceiling("cognitive_complexity", Path, Ceiling)
      - V > Ceiling
    then:
      finding: {path: Path, entity: Name, line: Line, value: V, ceiling: Ceiling}
```

The head names **which binding fills which `Finding` field**, rather than only a message. That is
not cosmetic: finding identity is `rule|path|entity` and the ratchet depends on it, so the mapping
has to be part of the rule syntax or section 5 is untestable. The message is generated from those
fields exactly as `Finding.__str__` does today.

Note that `Ceiling` is *bound by an atom* rather than computed inside the comparison. Every variable
used in a comparison, a negated atom or the head must be bound by a positive atom earlier in the
body. An earlier draft of this very example broke that rule -- it referenced `{Ceiling}` in the
message while computing `ceiling(...)` inline -- which is a fair indication of how easily a
"function call in a condition" surface stops being translatable.

A body is a conjunction of **atoms** (relation name + variable or literal per column), **comparisons**
between bound variables and literals or built-in functions, and **negated atoms**. There is no
disjunction: two rules with the same head are the disjunction, which is what Datalog does and why
the surface stays translatable. Every variable in a comparison, in a negated atom, or in the finding
template must be bound by a positive atom earlier in the body — the standard range-restriction
requirement, enforced at load time.

**Stratification is checked, not assumed.** Rules are grouped into strata by their negation
dependencies; a rule that negates a relation its own stratum derives is rejected at load time with
the offending pair named. Hand-rolled evaluation makes it trivially possible to write such a rule
and get plausible output, which is exactly how a surface stops being Datalog-compatible without
anyone noticing.

### 3. The evaluator is hand-rolled, and its shape is the migration plan

Bottom-up, semi-naive, one stratum at a time: seed with the EDB, apply rules until no new tuples
appear, move to the next stratum. Joins are hash joins on shared variables, with each relation
indexed on the columns rules actually bind. This is a few hundred lines and has one job — to be
replaceable. Swapping in a real Datalog engine means replacing `evaluate()` and nothing else,
because the rule syntax, the relation schema and the stratification rule are all specified here
rather than implied by the implementation.

`oxn[asp]` (clingo) and `oxn[smt]` (Z3) remain optional extras under
[ADR-0001](0001-dependency-policy.md) and are not part of this decision.

### 3a. A rule's scope is derived from the relations it touches

`entity`, `metric`, `contains` and `layer` are answerable for a single file from the store.
`imports` and `cycle` are not: the import graph is built by parsing every file in the tree, which
is why contracts are `--deep`-only today and why the hook does not pay for them.

So **scope is a property of the body, not a flag on the rule**: a rule touching only file-scoped
relations runs on the hook path; a rule touching `imports` or `cycle` runs only under `--deep`.
Deriving it rather than declaring it removes the failure where a repository-scoped rule is quietly
marked file-scoped and either never fires or costs the hook a full graph build. It also preserves
`_apply_baseline`'s existing files-versus-repository split, which section 5 depends on: a baseline
recorded under one scope is never compared against findings produced under the other.

### 4. The gate policy lives in the rule body, not beside it

ADR-0002 says only an `EXACT` value, or an `APPROX` value known to be a **lower bound**, may block a
ceiling. In a hand-coded check that is one `if`. In a rule engine it must be expressible, or every
rule an operator writes silently re-opens the hole:

```
- metric(E, Key, V, Ex, B)
- blocking(Ex, B, MayBlock)    # carried into the head, not used to filter
```

**Corrected during implementation, and worth recording.** The first draft made `blocking` a
*filtering* atom — a body that failed to join simply produced nothing. Writing the fact projection
against the hand-coded gate showed that is a behaviour change: `_ceiling_findings` emits a finding
for an unsound measurement and marks it advisory. "May not block" and "does not exist" are different
claims, and the second one loses the finding from the report. So the relation carries a third column
and the head binds it. The rows are projected from `MetricValue.can_block_ceiling` itself rather
than re-encoded, so the rules and the property cannot drift apart.

The same review found two relations the appendix had implied but not named. Datalog has no string
functions and no glob matching, so anything derived from values is computed at projection time:
`edge_label` carries the `"source -> target"` string a contract finding is keyed by, and
`entrypoint_match` carries the result of resolving a `deep_import` glob against the known paths.
Moving both out of rule bodies is what keeps every rule a pure join — and it makes a pattern that
matches nothing visible as an empty relation rather than as a rule that quietly never fires.

There is no `runtime` relation, though an earlier draft listed one. `build_dependency_graph` drops
type-only edges before the graph exists, so `imports` is runtime-only by construction and no rule
can forget the filter. That matters: this project once counted type-only imports as coupling and
manufactured two layer violations from them.

### 5. Finding identity is unchanged, because the ratchet depends on it

Rules emit the existing `Finding`, with the existing `rule|path|entity` key. A baseline written by
the hand-coded gate must be read by the rule engine with zero regressions and zero new findings on
an unchanged tree. Anything else silently resets every user's accepted debt — the ratchet is shared
state, and rewriting its keys is indistinguishable from an amnesty. This is a test, not an
intention.

### 6. ADR frontmatter compiles to the same rules

```yaml
constraints:
  ceilings: {cognitive_complexity: 8}
  forbidden-imports: ["oxn.graph -> oxn.metrics"]
```

`applies-to` already exists and already carries globs. Frontmatter constraints compile to exactly
the `Rule` shape above, scoped by those globs — the same engine, no second path. An ADR whose
`applies-to` matches **zero files** produces an advisory finding rather than silence: a constraint
governing nothing is almost always a typo or a stale path, and OXN has been bitten three times this
cycle by patterns that silently matched nothing (`shutil.ignore_patterns` basenames, the Java
`asterisk` node, a corpus path that ate three shell arguments). It is advisory rather than blocking
because an ADR may legitimately predate the code it governs.

## Consequences

**The hook path must stay inside its budget.** `oxn check` is a cold process at p95 ≤ 200 ms
(ADR-0002). A generic join evaluator is easily an order of magnitude slower than the direct store
read it replaces. `test_latency.py` must time the *rule* path once it is live, or the assertion
measures code nobody runs — the failure mode this project has hit four times in one cycle.

**Measured, and the fear was justified.** The first evaluator scanned every tuple of a relation for
every binding, which is what "join" means if nobody writes the index: **72 seconds for 60 files**,
and a 1,913-file corpus ran thirteen minutes without finishing. The section above said "hash joins
on the columns already bound" and the implementation simply did not have them. With the index:

| corpus | files | facts | before | after |
|---|---|---|---|---|
| `src` | 69 | 26k | 22.06 s | **0.07 s** |
| python-httpx | 60 | 44k | 72.20 s | **0.13 s** |
| typescript-nest | 1,913 | 639k | never finished | **3.39 s** |

Which columns to probe is a property of the *rule* rather than the data — the body is a fixed
sequence, so the variables bound when an atom is reached are the same for every binding flowing
into it. One index per (relation, probed columns) serves the whole evaluation. The property is
pinned by a test that counts how often a relation is read, because 555x is the kind of number that
quietly comes back.

**The hand-coded checks are the oracle and stay.** Old-versus-new findings must be identical
across `src` and every corpus.

*Amended 2026-09-04.* This paragraph originally read "stay importable **until** the exit criterion
is met", and scheduled a commit deleting them once parity held. That deletion is now declined. The
exit criterion is met -- parity is byte-identical on `python-httpx` (101 findings), `typescript-nest`
(656, 1,913 files) and the four-contract fixture -- but meeting it is what made the oracle
*load-bearing* rather than redundant: `tests/test_rules_corpus.py` and `tests/test_rules_parity.py`
assert parity against these functions, so deleting them deletes the property, not the duplication.
Two implementations of eleven checks is the cost of that property, and it is the price this project
already agreed to pay everywhere else -- the differential oracles of P2 run in CI forever for the
same reason. The duplication is also a live tripwire while P10 is still adding rules: a rule that
changes in `builtin.py` and not in the oracle fails parity loudly instead of shipping.

**Rules become a supported surface.** A malformed rule must fail loudly at load time with the rule
named — never be skipped, and never half-apply. `oxn.yaml` is already strict about unknown keys and
this extends that.

**What this does not do.** Constraint budgeting, BM25 retrieval and the typed constraint bundle are
P8. This ADR produces *rules*; deciding which subset of them an agent is shown is a different
problem with a different failure mode (constraint decay, ADR-0003).

## Appendix: every existing check, written against this schema

Written before the evaluator, because a check that cannot be expressed on paper is a missing
relation found at doc-edit cost rather than mid-implementation. All eleven fit.

**The six ceilings** are one shape with two parameters — the metric key and which entity kinds it
applies to. `function_sloc` and `file_sloc` share the metric key `sloc` and differ only in the kind
guard, which is exactly why they are two rules and not one:

```
ceiling_rule(Key, KindGuard):
    entity(E, Path, Kind, Name, Line)
    metric(E, Key, V, Ex, B)
    blocking(Ex, B)
    KindGuard(Kind)                       # callable(Kind) or file_kind(Kind)
    ceiling(RuleName, Path, Ceiling)
    V > Ceiling
  → finding{path: Path, entity: Name, line: Line, value: V, ceiling: Ceiling}
```

| rule | metric key | kind guard |
|---|---|---|
| `cognitive_complexity` | `cognitive_complexity` | `callable` |
| `cyclomatic_complexity` | `cyclomatic_complexity` | `callable` |
| `max_nesting_depth` | `max_nesting_depth` | `callable` |
| `parameter_count` | `parameter_count` | `callable` |
| `function_sloc` | `sloc` | `callable` |
| `file_sloc` | `sloc` | `file_kind` |

**Shredding** is the same shape against `shredding_cluster`, and it joins `ceiling` on
`cognitive_complexity` rather than on its own name — which is how the `Gate.follows` semantics
survive translation. The cluster *computation* stays in the metric engine: folding dedicated
helpers onto a root is a fixpoint over call sites that the store already answers, and expressing it
as a rule would be re-deriving a fact rather than joining one.

**The four contract kinds** are edge joins. Every one of them was read before writing this, and
**none needs reachability** — they all test a direct edge and use the edge itself as evidence.
`layered` compares positions in the declared order; `forbidden` and `independence` test membership;
`deep_import` tests a glob:

```
layered:        imports(S, T, K), runtime(K), layer(S, L1), layer(T, L2), L1 != L2,
                allowed_edge(C, L1, L2) absent
forbidden:      imports(S, T, K), runtime(K), layer(S, L1), layer(T, L2),
                forbidden_edge(C, L1, L2)
independence:   imports(S, T, K), runtime(K), layer(S, L1), layer(T, L2), L1 != L2,
                independent(C, L1), independent(C, L2)
deep_import:    imports(S, T, K), runtime(K), layer(T, P), package(C, P),
                layer(S, Q), Q != P, entrypoint(C, G), matches(T, G) absent
  → finding{path: S, entity: "S -> T", line: 1, value: 1, ceiling: 0}
```

The head reproduces today's edge-keyed identity exactly: `path` is the importing file and `entity`
is `source -> target`. Keying on the *layer pair* instead — which this project shipped once — makes
the baseline an amnesty, because a new violation between two layers lands on an existing entry with
an identical value and neither "new" nor "worse" can fire.

**Consequence for increment 1: nothing recurses.** `layered` is the only rule that could have
needed transitive closure and it does not. With no recursive rule in the system, semi-naive
evaluation degenerates to a single pass over each stratum, and the negation used (`absent`) is
stratified trivially — every negated relation is extensional. The evaluator still implements the
fixpoint loop and the stratification check, because the point is a surface a real Datalog engine
can take over; but this ADR records that **the first rule set exercises neither**, so the day a
recursive rule arrives is the day those paths are first tested, and they need a test written for
them rather than assumed working.

## Amendment, 2026-09-05 — `imports` is not repository-scoped, and the hook can afford it

Section 3a derives a rule's scope from the relations in its body, so that a rule cannot be
mislabelled. The derivation was right; one of the inputs was wrong.

`REPOSITORY_RELATIONS` held `{imports, cycle}`, and `rules_for_scope(deep=False)` therefore
dropped all four contract rules on the hook path. The stated reason was that `imports`
"only exists once the whole tree has been parsed". That is a fact about
`build_dependency_graph`'s *call site*, not about the relation: resolution needs the tree's
**layout** — which files exist, where the package roots are, what `tsconfig` aliases say —
and the importing file's **text**. Only the second is per-file work. Measured on
`typescript-nest`: the directory walk is 21 ms over 1,913 files, `ResolutionContext.build`
5 ms, and parsing one file for its imports 10 ms.

So the hook parses the edited file, resolves against the whole tree's layout, and evaluates
every contract rule against that file's outgoing edges. `REPOSITORY_RELATIONS` is now
`{cycle}` — the one relation genuinely unanswerable from a single file — and the scope
predicate becomes "which relations did this run populate" rather than "is this run deep":

```python
CONDITIONAL_RELATIONS = frozenset({"imports", "cycle"})


def rules_for_scope(settings, *, populated: frozenset[str]) -> list[Rule]:
    missing = CONDITIONAL_RELATIONS - populated
    return [rule for rule in all_rules(settings) if not (rule.relations & missing)]
```

Three consequences worth recording, two of them defects the change exposed in
`graph_facts` — both invisible while every run parsed everything.

**`layer` must be projected for both ends of an edge, not for the parsed files.** Every
contract rule joins `layer(Src, _)` *and* `layer(Dst, _)`. `assign_layers(sorted(graph.files))`
covers both only because a whole-tree run parses every target. Give it one file and the rule
cannot fire: a silent false negative, in the direction that looks like conformance.

**`_entrypoint_facts` must glob against both ends too.** A `deep_import` contract's
`allowed_entrypoints` are matched against known paths. Match them against the parsed files
and the hook finds no entry point, so every legal import into the package reports as a
bypass. An empty allow-list is not a strict check; it is a false positive on correct code.

**Finding identity is unchanged, and that is asserted rather than assumed** (section 5). The
hook and `--deep` produce byte-identical keys for the same edge, without which
`.oxn/baseline.json` would stop being a ratchet: an accepted layer violation would re-fire
as new the next time anyone touched the file.

That identity test is also the **only** oracle this path has, and the amendment above is
where that is written down rather than left to be discovered. The parity suite asserts the
rules against `check._measure` and `check._architecture`, the hand-coded checks kept as the
oracle -- and `_architecture` has no file-scoped mode, so it cannot answer what the hook now
answers. `--deep` stands in for it: the same repository, the same edge, the same key, from
two scopes. "Byte-identical to the hand-coded suite" remains true of every rule the
hand-coded suite can express, and this is the first path where that sentence needed a
qualifier.

The honest limit is that a file-scoped contract check sees the edges a file *makes*, never
the edges made *at* it. That is not a weakening — the file that made an illegal import had
its own edit gated — but it is a different claim from `--deep`, and `check` says so in a
diagnostic rather than leaving a caller to infer it from silence.
