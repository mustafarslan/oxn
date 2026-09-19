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
| `match/case` | 2 | 3 | **2** | Each `case` is a branch **except the catch-all**: `case _` is the fall-through, exactly like `default` in a switch. A `match` with one case and a wildcard therefore scores the same as the `if`/`else` it is equivalent to. Agrees with radon. |
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

## Import graph — Python (oracle: grimp)

**Agreement: 120/152 of grimp's edges (79%), and every divergence reduces to one of two
enumerated rules.** Raw equality was asserted until 2026-08-30 and passed only because OXN
had a bug; the two tools answer different questions in two places.

| divergence | grimp | **OXN** | why |
|---|---|---|---|
| import inside `if TYPE_CHECKING:` | counted (32 edges) | **excluded** | erased at runtime, so no coupling of the kind Martin's metrics measure |
| `from pkg import module` | `pkg.module` only | `pkg.module` **and** `pkg` (2 edges) | importing a submodule executes the package `__init__`; that dependency is real |

**On type-only imports.** OXN already excluded TypeScript's `import type`; Python has no such
keyword, and its equivalent is an ordinary import nested under a `TYPE_CHECKING` guard.
Counting one and not the other was an inconsistency, not a policy — and while it lasted it
inflated every Martin metric on Python code and manufactured **two false layer-contract
violations in OXN's own repository**, which is where it was found. `include_type_only=True`
still reports them for anyone who wants the compile-time graph.

**On package edges.** This is the same modelling decision that an earlier fixture-based
comparison with grimp established: treating `from pkg import core` as a single target
silently loses edges. OXN records both targets; grimp records the submodule.

The test now asserts every divergence falls under one of these two rules, plus a floor on
raw agreement so the rules cannot quietly come to explain everything.

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
| generator expression in `yield from (...)` | **missed** | counted |

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

### The largest divergence measured so far: 5 vs 26

Found in use rather than on the corpus, which is why it is recorded here separately. The repair
harness asked a model to simplify `_imported_names` (`src/oxn/resolve/scopes.py`); it replaced
nested loops with `yield from (genexp)` throughout. **complexipy scores the result 5. OXN scores
it 26.**

The gap is entirely the `yield from` row above. Reduced:

```python
def yielded(y):
    yield from ((a, b) for a in y for b in a if b if b > 1)
```

**complexipy: 0. OXN: 4** (`for` 1 + `for` 1 + two filters). A function with four decisions
scoring zero is a traversal gap, not a position on what comprehensions cost — and complexipy
charges the same clauses correctly when the identical expression appears in a `return`
(`plain_return` = 3, matching OXN).

Two controls make the verdict safe rather than self-serving:

* on the **original** function both tools return **exactly 35**;
* where complexipy can see the construct, its per-clause charges match OXN's rules.

So OXN stands. Recorded because a 21-point live divergence against the reference implementation
is precisely what this file exists to hold, and because the next person to see a 5 next to a 26
deserves to find the answer already written down.

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


---

## Logical lines — Python (oracle: radon)

**Clause counting, and only clause counting.** radon treats each clause of a compound
statement as its own logical line; OXN counts the statement once.

| source | radon | OXN |
|---|---|---|
| `if x:\n a = 1\nelse:\n b = 2` | 4 | 3 |
| `try:\n a = 1\nexcept E:\n b = 2` | 4 | 3 |
| `@deco\ndef f():\n return 1` | 3 | 2 |

Neither reading is canonical. An `else:` introduces no logical line of its own under the
SEI definition, and OXN already treats an `else` as continuing its `if` rather than nesting
under it in cognitive complexity (`_visit_alternative`) — counting it as a separate logical
line here would have the same construct answering two different ways in one tool. A
decorator is excluded for the same reason `unwrap` exists: `decorated_definition` holds the
`function_definition` that is the statement.

Everything else agrees. This was a **43% undercount** until 2026-09-10: `statement_kinds`
named `expression_statement`, the shipped Python grammar emits `block > assignment` and
`block > call` with no such wrapper, and the two commonest statements in the language
counted zero. Over OXN's own `src/`, lloc read 4,128 against radon's 9,582; it now reads
7,354, and the remaining gap is the table above.

The same change was first reported as buying 1.22x on Go. It buys **1.00x**: the Go
`if_statement` had been listed as a statement container, and it holds its condition and its
consequence block beside its init clause, so every `if` counted three logical lines instead
of one. Go and Java both sit at 1.00x, which is the reassuring shape -- their grammars wrap
statements the way `statement_kinds` expected, so nothing there was broken and nothing there
moves. The languages that genuinely gain are Python (1.78x), Rust (1.23x), TypeScript
(1.16x) and JavaScript (1.04x).

---

## Duplication — five languages (oracle: PMD-CPD 7.7.0)

**Recall, not equality.** The two tools tokenize differently and resolve overlapping
candidates differently, so identical clone classes were never a sensible target. What matters
for a duplication detector is what it *misses*.

**This comparison ran on one Python package until it ran on five languages, and widening it
found a bug.** `DUPLICATION_MIN_TOKENS` carried `observations=1` and a `fit_when` that said as
much. Measured on whole corpora at `--minimum-tokens 50`, both tools given an identical file
list (`--file-list`, not `--dir`, so a file one tool walks and the other skips cannot score as
a miss):

| corpus | language | files | PMD lines | OXN lines | recall before | **recall after** |
|---|---|---|---|---|---|---|
| python-httpx | Python | 60 | 2,481 | 4,475 | 0.858 | **0.902** |
| java-spring-petclinic | Java | 50 | 183 | 494 | 0.809 | **0.863** |
| go-kit | Go | 256 | 6,301 | 8,258 | 0.798 | **0.841** |
| javascript-eslint | JavaScript | 1,443 | 86,043 | 246,379 | 0.757 | **0.873** |
| typescript-nest | TypeScript | 1,901 | 29,982 | 45,614 | 0.691 | **0.818** |

**Rust is not in this table because PMD-CPD 7.7.0 ships no Rust lexer.** Five of the six
launch languages are checked against an oracle here and the sixth is unchecked, which is a
different statement from six.

**The bug the single corpus could not show.** Selection claims tokens longest-first so a short
clone cannot steal a long one's span — that part was already right, and lifted recall from 59%
to 92% when it was introduced. What was still wrong is how a class was *refused*: the moment
any one of its occurrences overlapped something already claimed, the whole class was dropped,
taking untouched occurrences with it. Two files that are byte-identical copies could be
reported as clean because a different pair of files shared a longer block containing the same
code. On spring-petclinic, 14 of 94 candidate classes had two or more free occurrences and
were being discarded for the sake of a third. Thinning a class to its free occurrences —
keeping it when two or more survive, since two is what makes duplication duplication — raised
recall in all five languages and none regressed.

Python was the language least able to reveal it (+0.044) and TypeScript the most (+0.127),
which is the argument for the parametrization rather than for the fix.

**Why recall is not 1.0, and will not be.** OXN reports one maximal *non-overlapping* class
where PMD reports several overlapping ones. Spring's three integration-test classes share an
eleven-line block; PMD reports it as two overlapping pairs both containing the Postgres copy,
while OXN gives those tokens to one class and the third copy has no free partner left to pair
with. That is a policy difference rather than a miss, and it sets a ceiling below 1.0 that no
amount of fixing removes. `tests/test_oracle_pmd.py` therefore floors recall per language, at
the measured value rounded down — one floor for all five would be either unreachable for
TypeScript or vacuous for Python.

**On the line counts.** OXN reports more duplicated lines than PMD everywhere, most sharply on
eslint (2.9x). Two causes, both deliberate: it extends every match maximally before selecting,
so one long clone replaces several short ones; and it normalizes identifiers, detecting Type-2
(renamed) clones where CPD's default settings compare identifier text and find only Type-1.
Line-level Jaccard is correspondingly low (0.29–0.57) and is not a quality signal here — a
tool that finds strictly more cannot score well on symmetric overlap.

**Not detected:** Type-3 (gapped) clones, where an inserted or deleted statement breaks the
run. Stated rather than hidden — a clone report that silently omits a category is worse than
one that says what it covers.

## Import graph — Python (oracle: grimp 3.16)

**100% edge-set agreement, no divergences**, on two independent packages:

| package | grimp edges | OXN edges | agreement |
|---|---|---|---|
| `oxn` | 76 | 76 | **100%** |
| `httpx` | 87 | 87 | **100%** |

This is stronger evidence than the number suggests, because the two tools work by entirely
different mechanisms: grimp *imports* the package and observes what Python loads; OXN reads
syntax and resolves specifiers against the file tree. Agreeing exactly, twice, means the
resolution rules are right rather than merely self-consistent.

**The comparison found a real modelling bug.** `from pkg import core` reaches the package
*and* the submodule — grimp reports edges to both `pkg` and `pkg.core`, because Python
executes both. OXN modelled one target per import, which silently lost an edge wherever the
package was not imported anywhere else. It matched by luck on the first corpus. Imports now
resolve to a *set* of targets.

### Java resolution

**Oracle-compared against jdeps** (`tests/test_oracle_jdeps.py`), 2026-09-17, over the 50
main and test classes of `java-spring-petclinic`: **jdeps 27 in-package edges, OXN 24, agreed
24 — 88.9%, and OXN is a strict subset.** The containment is the result, not the ratio:
**every edge OXN records, the compiler also resolved.** OXN invents nothing, which is the
direction that matters everywhere in this document — a wrong edge corrupts every downstream
metric silently, where a missing one is merely absent.

The other direction does not hold and cannot, because the two tools answer different
questions. **jdeps reads bytecode and sees the type the compiler resolved; OXN reads imports
and sees the type the source names.** Wherever inference or erasure puts a type in the class
file that never appeared in the source, no import exists for *any* import-based tool to find.
All three divergences are that one rule, each confirmed in the constant pool with `javap -v`:

| site | mechanism | the type never written down |
|---|---|---|
| `owner.getPet(name)` | the invoked method's return descriptor | `Pet` |
| `EntityUtils.getById(vets, Vet.class, 3)` | `<T extends BaseEntity>` erased into the descriptor | `BaseEntity` |
| `vet.getSpecialties().get(0).getName()` | `get` erases to `Object`, compiler inserts a checkcast | `Specialty` |

Two things are worth recording rather than rounding away. **The population is small for a
structural reason**: petclinic's classes depend on *Spring*, not on each other, so only 27
in-package edges exist at all — the rule above is exercised by exactly three of them. And
**`main` alone agrees perfectly**, 10 edges to 10 with no divergence in either direction; all
three divergences are in test sources, where fluent assertion chains over generic collections
are what produce inferred types in the first place.

The comparison also has a failure mode the other two do not, and it is guarded rather than
skipped: `target/` is a gitignored build artifact that can be from any commit, and jdeps on
stale bytecode looks exactly like an OXN resolver bug. The test asserts the compiled class
set equals the source set before comparing anything, and **fails** — naming `./mvnw -o
test-compile` — rather than skipping, because a skip would hide that forever.

### JavaScript resolution

**Oracle-compared against dependency-cruiser 18.3.1** (`tests/test_oracle_depcruise.py`),
2026-09-17, over all 1,487 files of `javascript-eslint`: **1,480 edges agreed** of the 1,489
OXN records and the 1,486 dependency-cruiser does — **98.997%**. Fifteen edges differ, and
each tool is more capable than the other on one of the three rules behind them.

| rule | who resolves it | sites |
|---|---|---|
| Node `main`, and workspace packages — `require("..")` → `lib/api.js` | dependency-cruiser | 6 |
| A workspace package name reaching an in-tree file, `require("eslint-config-eslint/cjs")` | OXN | 1 |
| TypeScript's emitted-name imports, `./helper.js` → `./helper.ts` | OXN | 8 |

OXN declines the first for the reason stated below for `exports` maps: a wrong edge corrupts
every downstream metric silently, where a missing one is merely absent. dependency-cruiser
misses the second because it needs the `node_modules` symlink npm would have created, and the
corpora have no dependencies installed.

**The comparison found one real defect on its first run**: imports inside a `.d.ts` were
recorded as runtime edges. A declaration file emits no JavaScript, so nothing loads it and
none of its imports is a runtime dependency — they carry no `type` keyword only because
inside a `.d.ts` there is nothing else they could be. Three edges on eslint, fixed in
`depgraph._add_imports`.

### TypeScript resolution

**Not oracle-compared, and the blocker has a version number.** The rule below — source imports
the *emitted* name, so `./x.js` means `./x.ts` — is the one that matters most, and
dependency-cruiser resolves it only via `enhancedResolveOptions.extensionAlias`, which
18.3.1's configuration schema does not accept. Every relative import in `typescript-nest` comes
back `couldNotResolve`, so the tool cannot arbitrate this corpus at all. A tool that cannot
resolve the corpus is not an oracle; TypeScript stays characterised rather than compared, and
no second tool was shopped for to get a different answer. Characterised on nest:
**99.9% of relative imports resolve** (3,600 of 3,603), 83.7% of all imports including
external packages. The three failures are generated files absent from the tree.

One rule matters more than all the others: **TypeScript source imports the *emitted* name**,
so `./x.js` refers to `./x.ts`. Missing it loses essentially every relative import in a
modern TS codebase — 402 of nest's 410, measured before the fix.

Deliberately **not** resolved, and reported as external rather than guessed at:
`package.json` `exports` maps, workspace globs, and symlinked monorepo packages. A wrong
edge is worse than one that says it is missing — the first corrupts every downstream metric
silently, the second appears in `unresolved_imports`.

---

## Architectural smells — no labelled corpus exists

Arcan's published smell labels are for **Java** corpora. There is no equivalent labelled
corpus for Python or TypeScript, so "reproduce published detections on a labelled corpus"
cannot be satisfied as written, and pretending otherwise would be theatre.

What OXN does instead:

* **exact assertions on constructed graphs** whose answers are computable by hand — a
  three-component cycle, a hub with balanced fan-in and fan-out of 6, an unstable dependency
  with hand-computed instabilities, a God Component against a known floor;
* **characterisation** on real code, reported rather than asserted.

Thresholds follow the literature where it states one (Degree of Unstable Dependency ≥ 30%,
Fontana et al. *ICSME 2016*; the 27,000-line God Component of Lippert & Rook) and are
otherwise OXN's own, marked as such in `src/oxn/thresholds.py`. Arcan's own defaults are
system-adaptive, derived from percentile analysis over the system plus a benchmark corpus.

**One threshold bug came out of testing this**: with fewer than ten components the p90 *is*
the maximum, so `size > p90` could never fire and God Components were undetectable in small
systems. Below ten components OXN now uses the fixed floor, because a percentile over three
points is not a percentile.


---

## Name resolution — SCIP (L2)

### The exit criterion had to be repaired before it could be claimed

The roadmap asked for "cross-file symbol resolution ≥98% precision at L2". That has no
denominator: precision of *what*, measured against *what*? SCIP **is** the ground truth at
L2 — there is nothing above it to check it against. The ≥98% figure properly belongs to
L0/L1 measured *against* L2, which is the next commit's work.

What L2 can honestly report about itself is **coverage**: how much of the code the index
actually reaches. Measured on httpx (60 files), with `scip-python` 0.6.6:

| | |
|---|---|
| index build | **4.4 s** (budget: 60 s) |
| ingest | 0.25 s |
| documents matched | 60 / 60 |
| **declarations matched to a symbol** | **99.0%** (1,228 symbols) |
| **call sites resolved to a target** | **96.1%** |
| edges recovered | 4,154 — 2,086 calls, 61 extends, 46 overrides pointing in-tree |

52.8% of edges point inside the tree; the rest name stdlib and third-party symbols, which
is correct rather than a miss.

### What SCIP buys that syntax cannot

In the fixture below, `run` calls `obj.save(1)` where `obj` is a local. Resolving that
requires knowing the local's inferred type — impossible from syntax alone, and exactly what
L0/L1 will have to approximate:

    calls     pkg.service.run                 -> `pkg.service`/SqlRepository#save().
    extends   pkg.service.SqlRepository       -> `pkg.base`/Repository#
    overrides pkg.service.SqlRepository.save  -> `pkg.base`/Repository#save().

### Three things discovered by running the tools rather than reading about them

1. **`scip-python` requires `--project-version`.** Without it, indexing dies inside
   `normalizeNameOrVersion` with `Cannot read properties of undefined`, an error naming
   nothing relevant. It is not in the documentation. `oxn index` always supplies one.
2. **An occurrence range is 3 elements when it stays on one line and 4 when it spans lines.**
   Handling only the 4-element form silently drops most occurrences in real code.
3. **A qualified callee must resolve on its *last* name component.** `obj.save(1)` starts at
   `obj`, which is usually a local variable, so matching the callee's start position yields
   an edge to the local instead of to the method. Caught by the fixture, not by inspection.

### Why the protobuf reader is hand-written

OXN needs six fields from a stable, versioned schema. The alternative is the `protobuf`
runtime plus a vendored generated module and a codegen step, for an *optional* feature —
ADR-0001 weighs install cost, not purity. Unknown fields are skipped **by wire type**, which
is what makes a future schema addition harmless. `protobuf` (free, Apache-2.0) remains the
fallback if the schema outgrows the reader, and swapping would change one module.

The reader is tested twice: against bytes constructed to exercise each shape, and — in the
oracle lane — against an index generated by the real `scip-python`. Synthetic bytes prove
the decoding; only a real artifact proves the field numbers.


---

## L0/L1 name resolution, measured against L2

The point of building L2 first: L0 and L1 can now be *graded* rather than described as
"approximate". Measured on httpx against a `scip-python` 0.6.6 index.

**Re-measured 2026-08-31 and reproduced exactly** — 1,453 graded call sites, 637 excluded,
953 confident answers of which 951 correct. Every figure below came back identical to three
decimal places, which matters for two separate reasons: the numbers ADR-0002's gating policy
rests on are reproducible rather than a one-off, and the `grade_file` refactor in this cycle
is verified against the authority rather than only against a synthetic differential.

| | precision | recall |
|---|---|---|
| every answer L1 offers | 70.3% | 100% |
| **only answers L1 is certain of** | **99.8%** | 65.6% |

L1 always has an opinion, but its confidence is `1 / candidates`: a unique in-scope
declaration is near-certain, a common method name resolves to many and it says so. **The
split is the gating policy** (ADR-0002): only certain answers may block, which recovers
L2-grade accuracy on two thirds of call sites with no toolchain at all. The remaining
misses are exactly what the design predicted — `client.get(...)` needs the receiver's
inferred type, and L1 picks one of the many `get` methods in the project.

### The oracle is wrong 30.5% of the time, and the measurement says so

`scip-python` 0.6.6 mis-resolves names re-exported through a package `__init__.py`, landing
on an **alphabetically adjacent** symbol:

| source writes | scip-python says |
|---|---|
| `httpx.Client(...)` | `AsyncClient` |
| `httpx.Response(...)` | `Request` |
| `wait_for(...)` | `sleep` |

Every case is adjacent in an import list — an off-by-one in re-export handling. Those call
sites are excluded from grading and counted, because a ground truth wrong one time in five
measures the oracle's defects rather than ours. Before that filter the same run reported
49.5% precision, which would have been a badly misleading number to publish.

The exclusion rate is itself asserted in CI: if it moves sharply, `scip-python` has changed
and the table needs revisiting.


---

## Tier 3: cohesion and coupling

### LCOM has six published definitions and they disagree

OXN computes all of them and labels which is which, because "LCOM = 4" means nothing without
saying whose LCOM. Two properties are worth stating before anyone reads a number:

* **A constructor that initialises every field joins every cluster.** A class that is really
  two classes still reports `LCOM3 = 1` if its `__init__` touches both halves. That is what
  the definition says, not a defect, and it is why LCOM\* is the headline rather than LCOM3.
* **LCOM\* exceeds 1 when fields are declared but no method touches them** — a data class
  with six attributes and two helpers scores about 1.7, bounded by 2. Clamping it to [0, 1]
  would discard a real signal about unused state.

### The exactness model gained a third state, and it is load-bearing

Cognitive complexity's specification increments for "each method in a recursion cycle,
whether direct or indirect". Without a resolved call graph only *direct* self-recursion is
visible, so any function that makes a call is **understated — never overstated**.

Stamping that plainly `APPROX` would have disabled this project's headline gate for almost
every non-trivial function, since only `EXACT` values may block. So metric values now carry
a `bound ∈ {exact, lower, upper}`, and the gate policy reads: *only `EXACT` values, or
`APPROX` values whose error has a known direction, may block.* A lower bound that already
exceeds a ceiling proves the true value exceeds it too.

### Only confident edges enter the call graph

A low-confidence L1 guess admitted to the call graph would invent a recursion cycle, and a
phantom cycle inflates the cognitive-complexity score of everything inside it — corrupting
the metric this project measures most carefully. Edges qualify only at L2, or at L1 where
the target was certain (99.8% precision, measured). Rejected edges are counted, not dropped.

### Dead code is characterised against vulture, never equated

vulture reports unused variables, imports and attributes as well as functions, and works
per-file with no call graph; OXN reports entities unreachable from declared roots. Only
unreached functions are comparable. OXN treats tests, `main`, and public module-level names
as roots — without that, a dead-code report on any real project is almost entirely noise.
Findings are **candidates** and never block: reflection, dependency injection and framework
entry points make false positives unavoidable.


---

## Six languages, one engine

The acceptance gate for a language profile is **agreement**, not parsing. Identical logic
transliterated into all six launch languages must score identically, and that test found
three bugs here that no single-language test could.

### A file that does not parse is a violation, not a silence

**5 of 5,580 corpus files fail to parse, 0.090%** (`scripts/measure_parse_errors.py`,
2026-09-17), which is what made it safe to let rule `unparseable` fail the gate. Until then a
file the parser could not read was counted as checked and clean -- and every pass drops such a
file without a word, so the clean answer was over a tree with the file missing.

The channel was decided by the hook contract rather than by taste. `report.errors` is the
semantically obvious home and exits `EXIT_ERROR`, which is 1; Claude Code's `PostToolUse`
blocks the edit and shows stderr to the agent only on exit **2**. A syntax error routed
through `errors` would be reported and not enforced, which is worse than the hole it closes
because it looks handled. So it is a blocking `Finding`, and it cannot be baselined: a
baseline means "accepted, and may not grow", which has no coherent reading here -- there is no
number to ratchet, and forgiving it would make every later run measure the tree without it.

**What it is not is a compiler.** Tree-sitter grammars are more permissive than the languages
they parse: `f(bar=1, 2)` is a positional argument after a keyword one, which CPython rejects
and the grammar accepts. The rule catches a file the *parser* cannot read, never a program
that will not run.

The residual 5 are two causes, and the honest caveat is the first:

| files | cause | false positive? |
|---|---|---|
| 2 (`typescript-nest`) | `@((...)())`, a parenthesised decorator the pack's TS grammar predates | **yes** |
| 3 (`adr-tessellation`) | `swagger-ui*.js`, vendored minified bundles, first line 184,623 chars | not source |

### One language, two grammars: `.tsx`

**Six launch languages, seven grammars.** `tree-sitter-typescript` ships `typescript` and
`tsx` separately, and the first cannot parse JSX at all. OXN mapped `.tsx` to `typescript`
until 2026-09-17, so **10 of the 11 `.tsx` files across the corpora failed to parse**; all 10
parse now.

What makes this worth a section is not the mapping but **how quietly it failed**. A file whose
tree has an error node is dropped by every pass that reads one — the import graph
(`depgraph.py`), duplication (`volume/scan.py`), the symbol index (`resolve/project.py`) and
resolution measurement (`resolve/measure.py`) — and none of them says so. The symptom was
therefore not an error. It was a React project reported as checked and clean, with its
components absent from the import graph.

`LanguageProfile.grammar` had existed from the start, on all six profiles, and was **read by
nothing**: every parse site called `get_parser(profile.name)`. That agreed with the grammar
for all six launch languages, which is why it survived — `.tsx` is the first case where the
label a metric groups by and the grammar that parses the file must differ. The `TSX` profile
keeps `name="typescript"` for exactly that reason; a phantom "tsx" language would split
TypeScript's published figures in half.

### Three grammars spell `else if` three different ways

| language | shape |
|---|---|
| Python | a dedicated `elif_clause` in the `alternative` field, alongside `else_clause` |
| TypeScript, JavaScript, Rust | `alternative` holds an `else_clause` that *contains* the next `if` |
| Go, Java | `alternative` holds the next `if` **directly** — there is no `else` node at all |

All three now route through one `alternative_style` setting, and the matrix asserts they
agree on five constructs including the three-level nest, the `else if` chain, and an
`else if` containing an `if`.

### Cyclomatic complexity vs lizard, per language

| language | compared | raw agreement | dominant divergence |
|---|---|---|---|
| Go | 1,877 | **100%** | — |
| Java | 29 | **100%** | — |
| TypeScript | 3,538 | 97.1% | closure attribution |
| Rust | 2,286 | 89.9% | the `?` operator, counted deliberately |
| Python | 1,134 | 56.2% | `assert`, 100% explained (see above) |

### Three bugs found by cross-language comparison

1. **Rust `if let` was counted twice** — once for the `if_expression` and once for the
   `let_condition`, doubling every `if let` in a Rust codebase.
2. **A catch-all branch was counted as a decision.** `case _`, `_ =>` and `default:` are the
   fall-through, not a branch. Counting them made a two-arm `match` score *higher* than the
   `if`/`else` it is exactly equivalent to. Fixed uniformly in Python, Rust and Java; the
   rule is now "a default is never a decision" in every language.
3. **Rust's `match` is exhaustive, and a `switch` is not.** The compiler guarantees a Rust
   `match` covers every case, so `n` arms give `n` paths and `n-1` decisions. A C-style
   `switch` has an implicit "nothing matched" path, so `n` cases give `n+1` paths and `n`
   decisions. Treating them alike overcounted every `match`. Fixing it took Rust from 80.7%
   to 89.9% agreement.

A fourth was found and *not* changed: OXN scores an `if` inside an `else` block as nested,
where gocognit collapses it into an `else if`. The white paper makes that collapse a
**COBOL-specific** exception, granted because COBOL has no `else if`. Go has one, so the
exception does not apply. Agreement with gocognit is 99.2% on 598 real Go functions, and
this is the only divergence class.

---

## Tests that call a language model

Almost none. The project's founding argument is that computing a metric with a
model gets you an approximation of something a parser knows exactly. So a model is never
used to *check* a metric — it is used to check the claims that are genuinely about a model's
behaviour.

The claim under test is the product's central one: that the explanation trail makes a
violation actionable where a bare score does not. Given the trail, the model names a line
the trail lists; given only the number, it correctly answers `UNKNOWN`. Run via Ollama
(`glm-5.3:cloud` by default, `OXN_OLLAMA_MODEL` to override), marked `llm`, and skipped when
the host is unreachable so the default lane never depends on a model being up.

One lesson from writing them: an *opinion* question ("is this actionable? YES/NO") produces
an opinion answer and a brittle test. Asserting **capability** — can the model name a line
it was given, and does it decline when it was not — is stable.

### The honesty check was licensed by an identifier

**A third of the numbers a model was permitted to state came from the commit sha.**
`quotable_numbers` walks the payload and takes the numerals out of every string -- which is
right for a finding's `message`, and wrong for `measured.commit`, a 40-character hash.
`d4e89d72e5c4e0206173e6dc3df513f8f131f126` contributed `126`, `131`, `206173`, `513`, `72`
and `89` to an allowed set of twenty, so "coverage rose to 206173" passed a check whose
docstring says the strictness is the point.

**Only the measurement side was tightened, and the asymmetry is load-bearing.** Prose keeps
the looser pattern: a writer who types digits is stating a number whatever they are glued to,
and applying the identifier exemption there would excuse `v2.1` and `Python 3.11` -- the two
inventions the rule already names. The sha never reaches the checked prose: the footer that
prints it belongs to `review_body`, OXN's own mechanical comment, not to the model's reply.

**A lookaround does not fix it**, which is worth recording because it is the obvious attempt.
`(?<![A-Za-z])\d[\d,]*(?![A-Za-z])` still yields `20617` and `26` from that sha: greedy
matching backtracks until the trailing lookahead sees a digit rather than a letter. The token
is the only unit at which "does this contain a letter" is a meaningful question.

It also surfaced as a **flaky test**, which is how it was found. The fixture commits afresh
each run, so a sha whose numerals happened to include `40` made a deliberately-dishonest
"Up 40%." legitimate and the refusal test fail: **3 runs in 150**, measured, and **0 in 200**
after the fix. The flake was the symptom; the leak was the defect.
