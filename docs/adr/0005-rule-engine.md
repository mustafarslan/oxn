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
| `blocking` | (exactness, bound) | ADR-0002's gate policy, as a two-row table |

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
- blocking(Ex, B)              # EXACT, or bound == "lower"
```

`blocking` is an ordinary relation. A rule that omits it produces advisory findings, and that is a
deliberate default: a rule cannot accidentally acquire blocking power by forgetting a clause.

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

**The hand-coded checks are the oracle and stay importable until the exit criterion is met.**
Old-versus-new findings must be identical across `src` and every corpus before anything is deleted,
and the deletion is its own commit.

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
