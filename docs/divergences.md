# Metric divergences

OXN implements its metrics itself (see [ADR-0001](adr/0001-dependency-policy.md)). The free tools it
deliberately does not depend on are used as **differential-test oracles in CI**, which is what makes
"self-implemented" a checkable claim rather than an assertion.

Those tools disagree with each other. This file records every known divergence, whether OXN is right
or merely different, and how each was established. It is generated from the same tables the tests
assert against, so it cannot quietly drift from the code.

All measurements: 2026-08-30, on `tree-sitter-language-pack` 1.15.8, radon 6.0.1, lizard 1.24.0,
complexipy (latest), eslint-plugin-sonarjs 4.2.0.

---

## Cyclomatic complexity — Python

**The oracles disagree with each other on five constructs.** There is no cross-language standard for
McCabe, so OXN publishes a rule and applies it consistently: *count what branches the control-flow
graph.* Measured on identical snippets:

| construct | radon | lizard | **OXN** | OXN's reasoning |
|---|---|---|---|---|
| `assert x` | 2 | 1 | **2** | `assert` raises or continues — a genuine branch. Agrees with radon. |
| `try/finally` | 1 | 2 | **1** | `finally` runs unconditionally; it branches nothing. Agrees with radon. |
| `for/else` | 3 | 2 | **2** | OXN counts no `else` of any kind, by rule. Agrees with lizard. |
| `match/case` | 2 | 3 | **3** | Each `case` is a branch. Agrees with lizard. |
| nested functions | parent 2 + closure 2, reported separately | flat, 4 total | **separate entities** | A nested function is its own entity in the code graph with its own score; a parent's number stays about the parent. |

Comprehension clauses (`for_in_clause`, `if_clause`) each add one decision point. radon and lizard
agree with OXN here and with each other — *except* when the comprehension sits inside an `assert`,
where radon counts neither clause: `assert all(x for x in a if x)` scores 4 in OXN and 2 in radon.
That single gap accounts for all six unexplained radon differences across httpx, and every one of
them is in a test file.

### Measured at corpus scale (httpx, 1,134 functions)

| oracle | raw agreement | **divergences explained by the rules above** |
|---|---|---|
| lizard | 56.2% | **100.00%** (1,134/1,134) |
| radon | 99.1% | **99.46%** (1,099/1,105) |

Raw agreement against lizard looks alarming until you see why: `assert` is pervasive in real Python,
and it is a one-line difference in the table above. **Raw agreement is the wrong measure**, because
radon and lizard contradict each other on constructs that appear in most files — no implementation
can agree with both. The measure that means something is whether every difference reduces to an
enumerated rule:

    lizard == oxn − (asserts) + (finally clauses)
    radon  == oxn + (loop-else clauses) − (extra match cases beyond the first)

Both formulas are asserted in `tests/test_oracles.py`. If an oracle changes behaviour, that test
fails and this document gets revisited.

**Harness consequence.** radon reports nested functions under `block.closures` rather than at top
level, while lizard reports them flat. Any oracle comparison must therefore match **per function by
name**, never by summing a file — otherwise the two tools appear to disagree when they do not.

---

## Cognitive complexity — Python (oracle: complexipy)

**Agreement: 651/656 functions = 99.24%** across all of httpx.

All five mismatches have a single cause: **complexipy does not detect comprehensions in certain
expression positions.** Characterised precisely:

| comprehension position | complexipy | **OXN** |
|---|---|---|
| `return [...]`, `x = [...]`, `g([...])`, `{'k': [...]}`, list/tuple literal | counted | counted |
| operand of a binary expression — `[1] + [x for x in a]` | **missed** | counted |
| inside an f-string — `f"{[x for x in a]}"` | **missed** | counted |
| keyword-argument value — `g(k=[x for x in a])` | **missed** | counted |
| inside a subscript — `d[[x for x in a][0]]` | **missed** | counted |

OXN counts a comprehension wherever it appears. The specification takes the same position for the
constructs it does discuss: increments apply "in variable assignments, method invocations, and return
statements" — that is, position-independently. **These are traversal gaps in complexipy, and OXN is
the more correct of the two.**

### Comprehension rules, derived rather than specified

The white paper says nothing about comprehensions, so OXN's rules were established by
characterising complexipy and are published as OXN's own (docs/metrics.md §3.2):

* every `for` clause is **structural** at the comprehension's own nesting — a second `for` in the
  same comprehension is *not* nested under the first;
* every filter `if` is **fundamental**, `+1`, never nesting-incremented;
* the comprehension body sits one level deeper, so a comprehension inside a comprehension nests.

Verified: `[x for x in a]` = 1 · `[x for x in a for y in b if x if y]` = 4 ·
`[[y for y in x] for x in a]` = 3 · `[[[z for z in y] for y in x] for x in a]` = 6.

---

## Cognitive complexity — JavaScript (oracle: eslint-plugin-sonarjs 4.2.0)

**Agreement: 14/18 cases.** Three of the four divergences are places where OXN follows the published
specification and the oracle does not.

| case | sonarjs | **OXN** | verdict |
|---|---|---|---|
| `if (a && b && c \|\| d \|\| e && f)` | 3 | **4** | **OXN follows the spec.** The white paper prints this exact example with `+1` for the `if` and `+1` per operator run, totalling 4. complexipy scores the Python equivalent 4 as well. Two implementations and the specification against one. |
| direct recursion | not counted | **1** | **OXN follows the spec.** Appendix B lists "each method in a recursion cycle" as an increment; sonarjs does not implement it. complexipy does, and agrees with OXN. |
| labelled `break` / `continue` | 4 | **4** | Agreement — but only after a fix. OXN initially scored 3, missing the rule that *labelled and multi-level* jumps increment while plain `break`, `continue` and early `return` do not. |
| function containing a nested function | reports each separately | **parent includes the nested body** | **Reporting granularity, not disagreement.** The specification's `myMethod2` example scores 2 for the *whole method*, including the lambda's nested `if`. OXN reports the parent's inclusive score and the lambda as its own entity, so the two must never be summed. |

---

## Cross-language equivalence

Python and TypeScript express `else if` completely differently — a dedicated `elif_clause` node
versus an `else_clause` wrapping an `if_statement` — yet must produce identical scores. Verified on
transliterated pairs: `if/elif` + nested `if` = 4 · `else` + nested `if` = 4 · plain chain = 3 ·
deep chain with a loop body = 8 · the three-level nest = 6 · `try`/`catch` with a branch in each = 4.

This test found two real bugs that single-language testing could not:

1. In TypeScript a `binary_expression` is also `a > 0`, so checking the node *kind* alone scored
   every comparison as a boolean sequence. Python's `boolean_operator` is a distinct node kind, which
   hid the bug entirely.
2. The `else if` collapse visited the inner branch at the wrong depth. The white paper's `toRegexp`
   example settles it: an `if` inside an `else if` body scores "+3 (nesting = 2)" beneath an outer
   `if` at nesting 1 — so the body *is* nesting-incremented even though the `else if` itself is not.

---

## Halstead — Python (oracle: radon)

**Not comparable by equality, and only weakly by rank.** On `def f(a, b): return a + b * 2` radon
reports `n1=2, N1=2`; OXN reports `n1=6, N1=6`. radon classifies only arithmetic and logical AST
operator nodes, ignoring punctuation, keywords and declarations; OXN classifies every leaf token per
its published table. Across httpx, Spearman ρ ≈ 0.82 for volume.

Neither is wrong — Halstead has no canonical classification in any language, which is exactly why
OXN publishes and versions its own (`halstead_spec_version`). CI asserts ρ ≥ 0.75 as a sanity check
that the two move together. Correctness rests on property tests and hand-computed goldens instead.
