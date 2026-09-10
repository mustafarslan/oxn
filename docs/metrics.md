# OXN Metric & Algorithm Catalogue

**Status:** design spec. **Companion to** [`../ROADMAP.md`](../ROADMAP.md) — the roadmap says *when*,
this document says *what and how*.
**Supersedes:** `idea.md` §3 (the radon subprocess shim) and §7 (the aspirational tree-sitter engine).
**Binding decisions:** [ADR-0001 dependency policy](adr/0001-dependency-policy.md),
[ADR-0002 resolution strategy](adr/0002-resolution-strategy.md),
[ADR-0003 enforcement model](adr/0003-enforcement-model.md).

Every metric below carries: definition and citation · required representation · language-agnostic
CST recipe · exactness · build-vs-adopt verdict · effort and risk.

---

## 0. Constraints, and what they force

### 0.1 The dependency rule

Free + open-source + easily integrable third-party tools are allowed as real runtime dependencies.
Anything commercial, paid, license-server-gated, or free-only-for-OSS must be self-implemented.
Licence compatibility with an MIT project is part of "easily integrable."

| Licence class | Runtime dep (shipped in the wheel) | Dev/CI oracle (subprocess, never distributed) |
|---|---|---|
| MIT / BSD / Apache-2.0 / ISC / UPL | allowed freely | yes |
| MPL-2.0 (file-level copyleft) | allowed as subprocess or unmodified dep | yes |
| LGPL | subprocess only, never linked or vendored | yes |
| GPL-3.0 / AGPL | never a runtime dep of an MIT wheel | yes — a CI subprocess is licence-clean (no distribution, no derivative work) |
| Commercial / license-server / free-only-for-OSS (SonarQube commercial editions, CodeScene, Sonargraph, Structure101, NDepend, Understand/SciTools, Lattix, CAST, Designite paid tiers, CodeQL on private repos) | no | no |

**Consequence:** every "self-implement" verdict below is justified on **install tax, hot-path latency,
cross-language uniformity, or output-schema control** — never on purity. Where a free tool is
genuinely better, we adopt it.

### 0.2 The constraint everyone forgets: the hook is a *cold process*

OXN runs in a Claude Code `PostToolUse` hook after every `Edit`/`Write`/`MultiEdit`. Therefore:

- **Every check is a fresh Python interpreter.** It cannot hold a warm LSP server, a warm JVM, a
  parsed project graph, or an in-memory index.
- Interpreter start alone is ~30–60 ms. Measured on the development machine (Python 3.14):
  `import networkx` costs **146 ms**. One careless top-level import consumes most of the budget.
- Agents edit files many times per task, so p95 hook latency decides whether OXN is used or disabled.

**Latency budget (design target):** p95 `oxn check <one file>` ≤ **200 ms** wall, **400 ms** hard
ceiling. Full-repo `oxn scan` on 100 kLOC ≤ 30 s.

Three architectural consequences that recur throughout:

1. **The OXN Code Graph persists on disk** — stdlib `sqlite3` (zero dependency, single file,
   transactional, mmap-friendly), keyed by `(path, content_sha)` for incremental invalidation.
2. **Anything needing a warm process (LSP, JVM) lives only in the long-lived MCP server**, never in
   the hook. The hook reads cache.
3. **SCIP's "static artifact on disk" model is a perfect fit for a cold hook** — an index file is a
   *lookup*, not a compilation. This is the decisive argument for SCIP over LSP as the primary
   precision layer.

**Tension to manage:** `idea.md` §5 promises "zero background daemons." Index refresh must therefore
be debounced work inside processes that already exist (the MCP server, an explicit `oxn index`, a CI
job) — never a daemon. Staleness is surfaced, never hidden.

---

## 1. The OXN Code Graph (OCG)

Every metric reads from the OCG. No metric touches tree-sitter directly. This is what makes metrics
language-agnostic, and makes "a new language" mean "a new `LanguageProfile`" and nothing else.

### 1.1 Node types

All nodes carry `id` (stable symbol id), `kind`, `name`, `qualified_name`, `lang`, `file_id`,
`byte_range`, `line_range`, and `resolution_level ∈ {L0, L1, L2}`.

| Node | Notes |
|---|---|
| `Repo` | root |
| `Component` | package / module directory / declared layer — the unit for Martin, Lakos, Arcan, DSM |
| `File` | physical file — unit for churn, hotspots, SLOC |
| `Module` | importable unit; may be 1:1 or n:1 with File (Rust `mod`, Java package) |
| `TypeDef` | class, interface, trait, struct, enum, protocol, type alias; attrs `is_abstract`, `is_exported` |
| `Function` | function, method, constructor, lambda, accessor; attrs `params[]`, `is_static`, `is_async`, `is_exported`, `decorators[]`, `receiver_name` |
| `Field` | instance or class attribute; attr `is_static` |
| `Variable` | module-level binding |
| `Parameter` | |
| `ExternalSymbol` | unresolved or third-party; attr `origin_spec` (raw import specifier) |
| `Author` | VCS identity, after mailmap normalization |

### 1.2 Edge types

Every edge carries `provenance ∈ {syntax, name_heuristic, scip, lsp, vcs}` and `confidence ∈ [0,1]`.

| Edge | From → To | Attributes |
|---|---|---|
| `CONTAINS` | any → any | tree backbone |
| `IMPORTS` | File/Module → Module/ExternalSymbol | `kind ∈ {value, type_only, side_effect, wildcard, dynamic}`, `alias`, `specifier_raw`, `resolved` |
| `DEPENDS_ON` | Component → Component | `weight` (edge multiplicity); derived from IMPORTS + REFERENCES |
| `EXTENDS` / `IMPLEMENTS` | TypeDef → TypeDef | |
| `OVERRIDES` | Function → Function | |
| `CALLS` | Function → Function/ExternalSymbol | `call_site_range`, `dispatch ∈ {static, virtual, dynamic}` |
| `READS` / `WRITES` | Function → Field/Variable | the LCOM substrate |
| `REFERENCES` | any → TypeDef | the CBO substrate |
| `PARAM_TYPE` / `RETURN_TYPE` | Function → TypeDef | L2 only |
| `CO_CHANGED` | File ↔ File | `support`, `confidence`, `jaccard` |
| `AUTHORED` | Author → File | `commits`, `added`, `deleted`, `last_touch` |

### 1.3 Metric storage — exactness is first-class

```
metrics(entity_id, metric_key, value, exactness ∈ {EXACT, APPROX},
        provenance, computed_at, input_sha, stale)
```

Every metric row carries its own exactness and provenance, which is what makes OXN's honesty claims
**machine-readable**. The JSON handed to the agent says
`"cbo": {"value": 14, "exactness": "APPROX", "provenance": "name_heuristic"}`. Gate policy (§10) can
then refuse to *block* on `APPROX` or `stale` metrics while still reporting them.

### 1.4 `LanguageProfile` contract

```
LanguageProfile:
  grammar, file_globs, shebangs
  node_kinds:
    function_like, class_like, comment_kinds, string_kinds
    decision_points: {node_kind -> weight_fn}                  # cyclomatic
    cognitive: {structural[], hybrid[], nesting_level[],
                nesting_increment[], ignored[], jump_kinds[]}
    boolean_ops: {node_kind, operator_field, ops[]}
    halstead: {operand_kinds[], operator_kinds[],
               excluded_tokens[], paired_delimiters[]}
    abstractness: {abstract_kinds[], abstract_markers[]}
  queries/*.scm:
    imports, definitions, references, fields, calls, entrypoints
  resolution:
    module_path_from_file(path, project_cfg) -> module_id
    resolve_import(spec, from_file, project_cfg) -> module_id | external
    receiver_name(fn_node) -> str | None     # the actual first param, not "self"
  exceptions:
    cognitive_exceptions[]                   # python_decorator, js_declarative_outer_fn, ...
```

**Adding a language = one profile + one golden corpus + one oracle binding.** Nothing else.

---

## 2. Name and type resolution

The highest-stakes subsystem, and per ADR-0002 an early phase rather than a research track.

| | (a) SCIP indexers | (b) stack-graphs | (c) LSP via multilspy | (d) Joern CPG | (e) our own resolver |
|---|---|---|---|---|---|
| Licence | Apache-2.0 | MIT | MIT client; servers vary | Apache-2.0 | ours (MIT) |
| Accuracy | Compiler-grade (scip-python→pyright, scip-typescript→tsc, scip-java→semanticdb, scip-go→go/packages, `rust-analyzer scip`) | Good name binding, **no types** | Compiler-grade, same engines | Deepest — CPG + interprocedural dataflow | Exact intra-file; heuristic cross-file |
| Coverage | Py, TS/JS, Java/Kotlin/Scala, Go, Ruby, Rust | Py, JS/TS, Java rulesets only | Any language with a server | C/C++, Java, JS/TS, Py, Go, Kotlin, PHP, C# | **every language with a profile — uniform** |
| Install | per-language toolchain (node/JDK/go/cargo) | Rust crate; **Python bindings are an unofficial WIP fork** | LS binaries per language | JVM + GBs | zero |
| Latency | index: s→min; **consume: file read, ms** | incremental per file, very fast | 1–10 s start, then ms — **needs a warm process** | minutes | sub-ms per file |
| Cold-hook compatible | **yes** (static artifact) | yes | **no** | **no** | yes |
| Offline | yes | yes | yes | yes | yes |

### 2.1 The four-rung ladder

**L0 — OXN scope resolver over the CST.** Always on, zero install. A lexical scope tree per file:
bindings, shadowing, parameters, comprehension and closure scopes, `self`/`this` receiver
identification, class member tables, import-alias tables. ~600–900 LOC of profile-driven traversal.
Design reference: scope graphs (Néron, Tolmach, Visser & Wachsmuth, ESOP 2015) and stack graphs
(Creager & van Antwerpen, EVCS 2023) — specifically their *file-at-a-time construction with
cross-file stitching*, which is exactly the incrementality shape a cold hook needs.

**L1 — OXN project symbol index in SQLite.** Always on. Stitches L0 per-file exports and imports
into a repo-wide symbol table: repo-internal class hierarchy, module graph, unique-name call
resolution.

**L2 — SCIP index.** Default-on wherever a toolchain is detected. `oxn init` probes for
node/go/JDK/cargo and records which indexers are available; `oxn index` runs them; the loader reads
the protobuf into SQLite. **Primary precision layer**, because it is (i) Apache-2.0, (ii) offline,
(iii) a *static artifact* — the only precise option a cold hook can consult — and (iv) one uniform
schema across N languages, matching the LanguageProfile philosophy exactly.

**L2′ — LSP via multilspy (MIT).** Fallback, inside the MCP server only. For languages with no SCIP
indexer, or when the user already runs pyright/gopls/rust-analyzer/jdtls. Never used by the hook.

**L3 — Joern (Apache-2.0).** Research track and differential oracle only. JVM, minutes. The right
tool for interprocedural dataflow experiments and for validating our call graphs; the wrong tool for
a gate.

**stack-graphs** is a design reference and an optional experimental backend — MIT and beautifully
suited in principle, but its Python bindings are an unofficial WIP fork and rulesets cover only
three or four languages. Revisit when bindings stabilise.

### 2.2 The SCIP → call-graph recipe (the linchpin)

SCIP occurrences do **not** mark "this reference is a call." The call graph comes from a **range
join** against the CST:

1. Load `Document.occurrences` into an interval tree keyed by byte range, value = `symbol`.
2. Walk the CST. For each node whose kind is in the profile's `call_kinds` (`call_expression`,
   `method_invocation`, `call`, `macro_invocation`, …), take the **callee child field**
   (`function`, `name`, `method`) and its byte range.
3. Look that range up in the occurrence interval tree → callee `symbol`.
4. Find the enclosing `Function` node → caller `symbol`, from the occurrence at the function's name
   range with `SymbolRole.Definition`.
5. Emit `CALLS(caller, callee, provenance=scip)`.

Type hierarchy comes free: `SymbolInformation.relationships` carries `is_implementation` →
`EXTENDS`/`IMPLEMENTS` edges directly. Verify per-indexer emission coverage; not all indexers
populate every relationship.

**Honesty clause that ships in the docs:** even with SCIP, virtual and dynamic dispatch resolve to
the *declared* target. Expanding to concrete implementations needs a CHA-style closure over
`is_implementation`, which over-approximates. Therefore `CBO` and `RFC` are **EXACT over the
resolved symbol set** but remain a *static approximation of runtime coupling*, and call-graph-derived
fan-in/fan-out is always `APPROX` for higher-order functions, reflection, DI containers and callbacks.

### 2.3 What becomes exact at each rung

| Metric | L0 only | +L1 | +L2 (SCIP) |
|---|---|---|---|
| Tier 1 (CC, cognitive, nesting, SLOC, params, Halstead, MI) | **EXACT** | EXACT | EXACT |
| Import graph, layers, Martin I/Ca/Ce, Lakos, DSM, Arcan | EXACT per file; module resolution heuristic | **EXACT** (modulo dynamic imports, always reported) | EXACT |
| Abstractness A | EXACT (syntactic) | EXACT | EXACT |
| WMC, NOM, class size | **EXACT** | EXACT | EXACT |
| LCOM1–5 / LCOM\* | EXACT for own declared fields | + inherited fields → **EXACT in-repo** | EXACT incl. external bases |
| DIT / NOC | in-file only | **EXACT in-repo**; external bases as depth+1 with a flag | **EXACT** |
| CBO / RFC | APPROX (name heuristic) | APPROX (import-scoped names) | **EXACT over resolved symbols** |
| Call graph, fan-in/out, recursion cycles, dead code | APPROX | APPROX | **EXACT-static** (declared targets) |

Cognitive complexity's *recursion increment* is the one Tier-1 rule that secretly needs Tier 3 — it
requires SCC detection on the call graph. **Ship direct self-recursion only** (name match in the
function's own scope), labelled `APPROX`, upgraded to EXACT once L2 lands.

---

## 3. Tier 1 — syntax-local, exact

### 3.1 Cyclomatic Complexity (CC)

**Definition.** McCabe, "A Complexity Measure," *IEEE TSE* SE-2(4), Dec 1976. `M = E − V + 2P`; for
structured programs `M = π + 1`, π = decision predicates.

**Representation.** Per-function CST. No resolution needed.

**CST recipe.** Base 1 per `Function`. Traverse the function subtree — *not* descending into nested
`Function` nodes, which get their own score — incrementing per the profile's `decision_points` table:

| Construct | Python | TS/JS | Go | Rust | Java |
|---|---|---|---|---|---|
| conditional | `if_statement`, `elif_clause`, `conditional_expression` | `if_statement`, `ternary_expression` | `if_statement` | `if_expression`, `if_let_expression`, `match_arm` | `if_statement`, `ternary_expression` |
| loop | `for_statement`, `while_statement`, comprehension `for_in_clause` + `if_clause` | `for_statement`, `for_in_statement`, `while_statement`, `do_statement` | `for_statement`, `range_clause` | `for_expression`, `while_expression`, `while_let_expression`, `loop_expression` | `for_statement`, `enhanced_for_statement`, `while_statement`, `do_statement` |
| multiway | (n/a) | `switch_case`, per case, not `default` | `expression_case` / `type_case` / `communication_case` | `match_arm` | `switch_block_statement_group` per `case` |
| exception | `except_clause` | `catch_clause` | (n/a) | `?` operator (`try_expression`) | `catch_clause` |
| boolean | `boolean_operator` (`and`/`or`) | `binary_expression` op `&&`, `\|\|` | `binary_expression` op `&&`, `\|\|` | `binary_expression` op `&&`, `\|\|` | `binary_expression` op `&&`, `\|\|` |

**Not counted:** `else`, `finally`, `default`, `try`, and null-coalescing (`??`, `?.`, `or None`
idioms) — a deliberate OXN normative choice, documented as a divergence.

**Edge cases.** Nested functions and lambdas get their own entry *and* are excluded from the parent's
count (radon aggregates differently — record as a divergence). Python `assert` and loop-`else` are
radon quirks; characterise empirically rather than assume. Rust's `?` is counted (+1), diverging from
Lizard; document it.

**Exactness.** EXACT under OXN's published decision-point table. There is no universal CC standard
across languages — **the table *is* the definition.**

**Build-vs-adopt.** Free options: **lizard** (MIT, pip, importable, ~20 languages, also gives
NLOC/token count/param count); **radon** (MIT, pip, Python only); **rust-code-analysis** (MPL-2.0,
tree-sitter based, CLI JSON); **gocyclo** (BSD-3, Go binary).
**Verdict: SELF-IMPLEMENT, with lizard + radon as differential oracles (HYBRID).** Not purity:
(i) we already parse the file with tree-sitter for cognitive complexity, nesting, Halstead and
imports, so computing CC in the same traversal is nearly free, whereas lizard re-lexes with its own
state machine and radon re-parses with Python `ast`; (ii) lizard's language support is
regex/keyword-based, not CST-based, so it cannot supply the node identity we need to attribute a
violation to an OCG entity with a byte range; (iii) one uniform decision-point table across all
languages is a product requirement, and lizard's per-language behaviours are not uniform;
(iv) subprocess-per-check kills the 200 ms budget.

**Effort/Risk.** S (2–3 d) / Low.

### 3.2 Cognitive Complexity

**Definition.** G. Ann Campbell, *Cognitive Complexity: a new way of measuring understandability*,
SonarSource white paper **v1.7, 29 August 2023**; peer-reviewed as Campbell, "Cognitive complexity:
an overview and evaluation," *TechDebt '18*.

Three basic rules: ignore shorthand; +1 per break in linear flow; increment when flow-breaking
structures are nested. Four increment types: **Nesting**, **Structural** (nesting-incremented *and*
raises nesting count), **Fundamental** (no nesting increment), **Hybrid** (no nesting increment, but
raises nesting count).

**Correction, verified against the primary source (v1.7, 29 Aug 2023).** "Ignore shorthand" concerns
**null-coalescing operators** and the method structure itself — *not* comprehensions. `idea.md`'s
paraphrase ("list comprehensions without filters") does not appear in the specification, which is
silent on comprehensions entirely. OXN's comprehension rules are therefore derived empirically
against complexipy (§3.2.1) rather than claimed from the spec.

**Appendix B spec:**

- **B1 — increments (+1 each):** `if`, `else if`, `else`, ternary · `switch` · `for`, `foreach` ·
  `while`, `do while` · `catch` · `goto LABEL`, `break LABEL`, `continue LABEL`, `break NUMBER`,
  `continue NUMBER` · sequences of binary logical operators · **each method in a recursion cycle**.
  Note the jump rule precisely: only *labelled* and *multi-level* jumps increment. A plain `break`,
  `continue` or early `return` does not — "because an early `return` can often make code much
  clearer". Python has no labelled break, so Python never receives a jump increment.
- **B2 — increments the nesting level:** `if`, `else if`, `else`, ternary · `switch` · `for`,
  `foreach` · `while`, `do while` · `catch` · **nested methods and method-like structures such as
  lambdas**.
- **B3 — receives a nesting increment (`+1 + current_nesting`):** `if`, ternary · `switch` · `for`,
  `foreach` · `while`, `do while` · `catch`.

The asymmetry that trips every reimplementation: **`else`/`else if` are in B1 and B2 but *not* B3**
(hybrid — the mental cost was already paid reading the `if`). **Lambdas and nested methods are in B2
only** (+0 structural, +1 nesting). **`try` and `finally` are ignored entirely.** A `catch` is +1
*regardless of how many exception types it catches*. A `switch` and all its cases together incur
**one** increment.

**The boolean-operator-sequence rule.** No increment per operator; **one fundamental increment per
sequence of like operators**:

```
score_bool(node):
  ops, leaves = [], []
  descend(node):                       # descend only through binary nodes whose op ∈ {&&, ||}
     if is_bool_binop(node): descend(lhs); ops.append(op); descend(rhs)
     else: leaves.append(node)         # parens, !, calls, etc. = sequence boundary
  total = number of maximal runs of identical operators in ops, in source order
  for leaf in leaves: total += score_bool(leaf)    # a NEW sequence context
  return total
```

Verified against the white paper:
- `a && b && c` → 1 run → **+1**; `a || b || c || d` → **+1**.
- `if (a && b && c || d || e && f)` → ops `&& && || || &&` → 3 runs → **+3**, plus +1 for the `if` = **4**.
- `if (a && !(b && c))` → outer sequence +1; the `!(…)` is a leaf, recursed as a new sequence +1;
  plus +1 for the `if` = **3**.

Increments apply to boolean sequences **anywhere** — assignments, arguments, return statements — not
only in conditions.

**`else if` detection (critical edge case).** *Verified against the actual grammars, 2026-08-29 —
the shape is not what it is usually described as.*

Python: an `if_statement` carries **multiple `alternative` fields**; `elif_clause` and `else_clause`
are siblings, both under the field name `alternative`. So read them with
`children_by_field_name("alternative")` (a list), not `child_by_field_name` (which returns only the
first). Each `elif_clause` is a hybrid increment.

TS/JS: the else-branch is **always an `else_clause`** — it is never an `if_statement` directly. An
`else if` is an `else_clause` whose named child is an `if_statement` rather than a `statement_block`.
The rule is therefore *look inside the `else_clause`*, not *test the alternative node's type*.

In both cases the hybrid treatment is the same: **+1, no nesting increment, and the consequent nests
at the same level as the original `if`.** Getting this wrong inflates the score on every `else if`
chain.

**Python comprehensions — derived, not specified.** The white paper says nothing about
comprehensions, so OXN's rules were established by characterising complexipy (a faithful MIT
implementation) across the clause combinations, and they are stated here as OXN's own:

| construct | rule | evidence |
|---|---|---|
| `for_in_clause` | **structural** — `+1 + nesting`, and raises the nesting level | `[x for x in a]` = 1; `[[y for y in x] for x in a]` = 3 (inner `for` is nested) |
| `if_clause` (filter) | **fundamental** — `+1`, no nesting increment | `[x for x in a if x]` = 2; `[x for x in a for y in b if x if y]` = 4 |
| comprehension under an `if` | inherits the outer nesting | `if a: [x for x in a if x]` = 4 (`if` 1 + `for` 2 + filter 1) |

Dict comprehensions and generator expressions follow the same rules.

**Documented compensating exceptions (Appendix A) that OXN must implement:**
- **Python decorators:** a function whose body is *only* a nested function definition plus a `return`
  does **not** increment the nesting level. (White paper's trio: `a_decorator` = 1,
  `not_a_decorator` = 2, `decorator_generator` = 1.)
- **JS declarative outer functions:** an outer function containing only declarations at its top level
  is ignored (no nesting increment); if it contains any statement subject to a structural increment
  at top level, it gets standard treatment.

**Representation.** Per-function CST, plus the call graph for the recursion rule.

**Exactness.** **EXACT** for all rules except the recursion increment, which is **APPROX (direct
self-recursion only)** until L2. OXN's per-language B1/B2/B3 tables are the operational definition;
SonarQube's own analysers differ slightly per language.

**Build-vs-adopt.** Free options: **complexipy** (MIT, pip, Rust core, Python only); **gocognit**
(MIT, Go binary, Go only); **eslint-plugin-sonarjs** `cognitive-complexity` (**LGPL-3.0**, needs Node
+ ESLint, JS/TS only); **rust-code-analysis** (MPL-2.0, ~10 languages, CLI). No free tool covers all
five launch languages in-process.
**Verdict: SELF-IMPLEMENT; all four as differential oracles (HYBRID).** No single free tool spans the
launch set; three of the four are non-Python subprocesses at 100–800 ms each, fatal for the hook;
eslint-plugin-sonarjs is LGPL and drags a Node toolchain. And cognitive complexity is OXN's headline
gate — owning the implementation is what lets us attach byte-precise per-increment explanations
("+3 at line 42: `if` nested 2 deep") to the diagnostic the agent must act on. **That explanation
payload is the product.**

**Effort/Risk.** M (5–8 d) / **Medium-High** — the hybrid/`else if` and boolean-sequence rules are
where reimplementations diverge. Mitigated by the golden corpus in §9.1.

### 3.3 Nesting depth

**Definition.** Max block-nesting level within a function. No canonical paper; nearest ancestors are
Dunsmore & Gannon (1979) on nesting and comprehension, and Hindle, Godfrey & Holt, "Reading Beside
the Lines: Indentation as a Proxy for Complexity Metrics," *ICPC 2008*.

**CST recipe.** Reuse the cognitive B2 `nesting_level` set; DFS tracking max depth. Emit two
variants: `max_nesting` and `avg_nesting` (mean depth of leaf statements).

Reuse the **rule** and not only the set. An `else` branch continues its `if` rather than nesting
under it — section 3.2's hybrid class — and re-deriving the traversal from the node set alone missed
that: TypeScript and Rust wrap `else if` in an `else_clause`, so the wrapper *and* the `if` it holds
each added a level, and identical logic read depth 4 there against 3 in Python, Java and Go. On a
gated ceiling that means code rejected for how its language spells `else`. Measured cost fell an
order of magnitude when it was fixed — ripgrep 0.96% → 0.10%, nest 0.28% → 0.02%.

**Exactness.** EXACT. **Verdict: SELF-IMPLEMENT** (free-rides the cognitive traversal).
**Effort/Risk:** S (0.5 d) / Low.

### 3.4 SLOC / LLOC / CLOC / blank / comment density

**Definition.** Physical LOC, logical LOC (statement count), comment LOC, blank. Counting rules
standardised by SEI: Park, *Software Size Measurement: A Framework for Counting Source Statements*,
CMU/SEI-92-TR-020.

**CST recipe.** SLOC = lines with at least one non-comment, non-whitespace token, derived from the
**token stream's line spans, not regex** — which correctly handles multi-line strings, JSX, raw
strings and heredocs. CLOC = union of line spans of nodes in `comment_kinds`; a line with code and a
trailing comment counts in both. LLOC = count of nodes standing where a *statement* stands:
a direct, named child of one of the profile's `statement_containers`, or a node whose kind is in
`statement_kinds`. Blank = total − (SLOC ∪ CLOC). Comment density = CLOC / (SLOC + CLOC).

**Kind alone is not enough, and assuming it was cost 43% of the count.** A grammar is under no
obligation to wrap a statement in a node named for its being one: the Python grammar OXN ships
emits `block > assignment` and `block > call` directly, with no `expression_statement`, so the two
commonest statements in the language matched nothing and scored zero — two bare calls on two lines
counted one logical line between them. Measured against radon over OXN's own `src/` on 2026-09-10:
4,128 against 9,582. The kind test is kept beside the position test because the two fail in
opposite directions, a wrapping grammar satisfying both around one line and a flattening one
satisfying neither; a node matching both is still one line. Wrappers (`decorated_definition`,
`export_statement`) and unnamed punctuation are excluded — a block's own braces are its direct
children, which made Rust's `{ let b = a; return b; }` count five.

A statement that is the *sole* content of another statement is one statement, not two. Rust writes
`return a;` as a `return_expression` inside an `expression_statement` and both kinds are counted, so
a function whose whole body was one return reported LLOC 3 against Go's and Python's 2. LLOC still
differs across languages after that, and should: it counts statements in the text, and an
expression-oriented language genuinely has a different number of them.

**Edge cases.** A Python docstring is a bare string in *statement* position, and the flag
`docstrings_are_comments` counts it as CLOC, matching radon's `multi` handling. **It is not
`expression_statement > string`** — this document said so, the implementation tested for it, and the
shipped grammar emits the string directly under its `block`, so the flag was set, was read, and
could never match. Every Python docstring counted as code until 2026-09-10: over `src/`, 4,577
lines — 27.7% of what OXN called code in its own source — with `comment_density` reading near zero
on files that are more prose than statements. Since `function_sloc` and `file_sloc` are gated, the
gate was charging this repository for documenting itself. The test is now statement position
(`block`, `module` or `expression_statement`), because which of the three a grammar emits is a
grammar-version detail a metric must not depend on. Multi-line strings
used as data must not count as comments. Normalise CRLF. Handle files with no trailing newline.

**Exactness.** EXACT under the published rule set.
**Build-vs-adopt.** Free: `cloc` (GPL-2.0, Perl), `scc` (MIT, Go), `tokei` (MIT/Apache, Rust), lizard
NLOC (MIT), radon raw (MIT). **Verdict: SELF-IMPLEMENT** — ~80 LOC off a token stream we already
have, and every alternative is a subprocess. `scc` and radon are the oracles.
**Effort/Risk:** S (1 d) / Low.

### 3.5 Parameter count, function length, file length, return count

**Definition.** Long Parameter List is a canonical Fowler smell (*Refactoring*, 1999/2018); the ≤4–5
threshold traces to Martin, *Clean Code* (2008) and Sonar's default. NEXITS (return count) comes from
rust-code-analysis and the single-exit tradition.

**CST recipe.** `Function.params[]` from the profile's parameter query, which must handle: defaults;
`*args`/`**kwargs`; TS optional and rest params; **Go variadic plus multiple names per type**
(`func f(a, b int)` = 2 params); Rust `self` receiver (excluded); Java varargs; destructured params
(OXN normative: **count as 1**); decorator-injected params (invisible — approximation). Function
length = `line_range` span, plus a `body_sloc` variant excluding signature and docstring.

**Rust leaves without a keyword, twice**, and NEXITS must count both. A function ends on its body's
final expression, and `?` leaves on the error path — Go spells that same control flow
`if err != nil { return err }`, which has always been counted, so omitting `?` made the two
incomparable on the most common error shape either language has. The tail is exact rather than
guessed: a block's statements are statement nodes, so a final child that is not one can only be the
tail expression. Transliterating one function into six languages read 4 exits in Rust against 5
everywhere else, and the missing one was the `-1` on the last line.

**Exactness.** EXACT. **Verdict: SELF-IMPLEMENT**; **lizard** (MIT) is the oracle for param count and
NLOC across all launch languages. **Effort/Risk:** S (1 d) / Low.

### 3.6 Halstead suite — the hard one

**Definition.** Halstead, *Elements of Software Science*, Elsevier, 1977.

n₁ = distinct operators, n₂ = distinct operands, N₁ = total operators, N₂ = total operands.
Vocabulary `n = n₁+n₂` · Length `N = N₁+N₂` · Volume `V = N·log₂(n)` ·
Difficulty `D = (n₁/2)·(N₂/n₂)` · Effort `E = D·V` · Time `T = E/18` s · Bugs `B = V/3000`
(Halstead's original: `E^(2/3)/3000`) · Estimated length `N̂ = n₁log₂n₁ + n₂log₂n₂`.

**The problem.** *There is no canonical operator/operand classification, in any language.* radon,
lizard, rust-code-analysis and every textbook disagree. Halstead defined it for Fortran. OXN must
therefore **publish its own normative table** and treat oracles as correlation checks, never equality.

**OXN normative classification policy v1** (per-language table in `LanguageProfile.halstead`):

1. Operate on the **leaf tokens** of the function subtree. Tree-sitter's named/anonymous distinction
   is the primary signal: *anonymous* leaves (punctuation, keywords) are operator candidates; *named*
   leaves (`identifier`, literals, `type_identifier`) are operand candidates.
2. **Operands:** identifiers; all literals (string, number, bool, nil); type identifiers when
   `include_type_annotations = true` (the default); member names after `.`; label names.
3. **Operators:** all arithmetic, comparison, logical, bitwise and assignment tokens; control-flow
   keywords (`if`, `else`, `while`, `for`, `return`, `try`, `def`/`function`/`fn`/`func`,
   `class`/`struct`/`impl`); `.` member access; `,`; `;`; `:` (Python block colon, TS type colon);
   `->`/`=>`; `await`, `yield`, `new`, `throw`, `raise`.
4. **Paired delimiters count as ONE operator per pair** — `()`, `[]`, `{}` — Halstead's own
   convention. A call's `()` is the *call operator*; the callee identifier is an **operand**
   (radon-compatible choice).
5. **Multi-token operators normalise to one:** Python `not in`, `is not`; Rust `as`; TS `as const`.
6. **Excluded entirely:** comments; whitespace; Python `INDENT`/`DEDENT`/`NEWLINE`; string
   interpolation delimiters (interpolation *contents* are analysed); `ERROR`/`MISSING` nodes, which
   set `parse_incomplete = true` and suppress the metric. The decorator `@` counts as an operator.
7. **Guards:** `n₂ = 0 → D = 0`; `n = 0 → V = 0`. `n₁ = 0` is impossible for a non-empty function —
   the `def` keyword is an operator.

**Exactness.** **EXACT with respect to OXN's published table; APPROXIMATE as a reproduction of any
other tool's Halstead.** This must be stated loudly — it is the single most likely source of "OXN's
numbers are wrong" reports.

**Build-vs-adopt.** Free: **radon** (MIT, Python only); **rust-code-analysis** (MPL-2.0, ~10
languages, tree-sitter based — *the closest prior art for exactly this problem*, and worth reading
its per-grammar operator tables as a design reference). **Verdict: SELF-IMPLEMENT (HYBRID)** — the
classification table must be *ours and uniform* for cross-language comparability, which is the whole
point of the polyglot engine; adopting radon would give Python-only numbers incomparable with our Go
numbers. rust-code-analysis is the multi-language oracle and radon the Python oracle, but assertions are
**rank correlation only**, never equality.

**Measured, 2026-08-30 — radon is a weak Halstead oracle, and the reason matters.** On
`def f(a, b): return a + b * 2`, radon reports `n1=2, N1=2` (only `+` and `*`); OXN reports
`n1=6, N1=6` (`def`, `(`, `,`, `return`, `+`, `*`). radon classifies only *arithmetic and logical
AST operator nodes*, ignoring punctuation, keywords and declarations entirely. Across httpx the
Spearman correlation of volume is **ρ ≈ 0.82**, and radon's per-file volume tops out around 2,500
where OXN's reaches 64,000. That gap is two different definitions, not an error in either. The CI
assertion is therefore `ρ ≥ 0.75` against radon — a sanity check that the two move together, not
evidence of agreement. **rust-code-analysis, being tree-sitter-based, is the oracle worth adding**
when a Rust CLI is acceptable in the oracle lane; correctness rests meanwhile on the property tests
of §9.3 and hand-computed goldens.

**Effort/Risk.** M (4–6 d) / **High** — the highest-risk Tier 1 item, entirely because of
classification ambiguity. Mitigation: ship the table as a documented, versioned artifact
(`halstead_spec_version: 1`) and never change it silently.

### 3.7 Maintainability Index — and its criticisms

**Definition.** Oman & Hagemeister, "Metrics for assessing a software system's maintainability,"
*ICSM 1992*; refined in Coleman, Ash, Lowther & Oman, "Using metrics to evaluate software system
maintainability," *IEEE Computer* 27(8), 1994.

- **Original / SEI three-metric:** `MI = 171 − 5.2·ln(V) − 0.23·G − 16.2·ln(LOC)`
- **SEI four-metric (with comments):** `+ 50·sin(√(2.4·perCM))`
- **Microsoft / Visual Studio rescaled:** `MI_VS = max(0, 100·MI / 171)`
- **radon's variant:** the four-metric form rescaled to 0–100, with `√(2.46·perCM)`

where V = Halstead Volume, G = cyclomatic complexity, LOC = SLOC, perCM = comment ratio.

**Criticisms, which ship alongside the number:**
1. Coefficients were regression-fitted to a **small 1990s Hewlett-Packard corpus of Fortran and
   Pascal**. There is no evidence they transfer to Python, TypeScript, Go or Rust.
2. It inherits Halstead's classification ambiguity (§3.6), so MI is only as reproducible as V.
3. Sjøberg, Anda & Mockus, "Questioning software maintenance metrics: a comparative case study,"
   *ESEM 2012* — MI and its constituents were poor predictors of actual maintenance effort.
4. Heitlager, Kuipers & Visser, "A Practical Model for Measuring Maintainability," *QUATIC 2007* —
   the SIG model was explicitly designed as a *replacement* for MI, on the grounds that a single
   opaque number gives no repair advice.
5. The comment term rewards comment volume, which is trivially gameable — a fatal property when the
   thing being governed is an LLM agent.

**OXN position:** compute and report MI (three variants, labelled), **never gate on it**, and put the
SIG-style risk profile (§10) in the gate instead. MI exists as a compatibility feature, not a quality
signal.

**Exactness.** Formula EXACT; inputs carry Halstead's approximation. **Verdict: SELF-IMPLEMENT**
(3 lines once V, G, LOC exist); radon and rust-code-analysis as oracles.
**Effort/Risk:** S (0.5 d) / Low algorithmically, **High reputationally** if presented as
authoritative.

---

## 3A. Tier 1.5 — volume, duplication and erosion trajectory

Syntax-local and exact like Tier 1, but repo-scoped rather than per-function. This tier exists
because it measures **precisely what LLM agents degrade**: "AI-Generated Smells" (arXiv 2605.02741)
found code volume correlated with architectural decay at ρ = 0.94 while prompt specificity had zero
effect (p > 0.8), and SlopCodeBench (arXiv 2603.24755) tracks exactly these two trajectory signals —
agent code is 2.2× more verbose than comparable open-source projects, with erosion rising in 80% of
trajectories and verbosity in 89.8%. Computing them natively makes the evaluation phase nearly free.

### 3A.1 Duplication / clone detection

**Definition.** Type-1 (exact) and Type-2 (renamed identifiers/literals) clone detection over
normalised token streams — the approach used by PMD's CPD and by Baker's `dup` (Baker, "On finding
duplication and near-duplication in large software systems," *WCRE 1995*); survey: Roy, Cordy &
Koschke, "Comparison and evaluation of code clone detection techniques and tools," *SCP* 74(7), 2009.

**Representation.** Per-file token stream from the CST. No resolution needed.

**CST recipe.**
1. **Normalise** each function's token stream: keep operator and keyword tokens verbatim; replace
   every identifier with a placeholder `$ID` and every literal with `$LIT` (this is what turns Type-1
   into Type-2 detection). Drop comments and whitespace. The profile's existing Halstead
   operator/operand tables supply the classification.
2. **Rolling hash** (Rabin–Karp) over a sliding window of `min_tokens` (default 50, PMD-CPD's default
   is 100 — OXN uses 50 with a `min_lines` guard, configurable). Bucket candidate windows by hash.
3. **Verify** each bucket by exact token comparison, to eliminate hash collisions.
4. **Extend maximally** in both directions, then greedily select non-overlapping maximal clones.
5. Emit clone *classes* (groups of clone instances), each with token length, line span, and the
   participating entity ids.

A suffix automaton over the concatenated normalised stream is the alternative implementation and
gives maximal repeats directly; Rabin–Karp is recommended for the MVP because it is incremental —
per-file window hashes cache in SQLite alongside everything else, so a single-file edit only
re-hashes that file.

**Edge cases.** Generated code and vendored directories must be excluded or duplication dominates
everything. Test files are usually excluded from the *gate* but reported. Boilerplate that is
genuinely idiomatic (Go error handling, TS barrel files) needs a `min_tokens` high enough not to fire
on it — calibrate empirically per language rather than assuming one threshold.

**Exactness.** EXACT for Type-1 and Type-2 clones over the published normalisation. Type-3 (gapped)
clones are **not** detected, and that limitation is stated rather than hidden.

**Build-vs-adopt.** Free options: **PMD-CPD** (BSD-style, JVM, ~20 languages, the reference
implementation); **jscpd** (MIT, Node); **simian** (**commercial** — excluded by the cost rule).
**Verdict: SELF-IMPLEMENT (HYBRID).** We already hold the normalised token stream from Halstead, so
the marginal cost is a rolling hash; PMD-CPD is a JVM subprocess that cannot run in the hook; and the
clone must be attributed to an OCG entity with a byte range, which CPD's output does not give us.
**PMD-CPD is the CI oracle**, asserted on clone-class equality within a tolerance for boundary
extension.

**Effort/Risk.** M (3–4 d) / Medium — the risk is threshold calibration, not the algorithm.

### 3A.2 Verbosity

**Definition.** The fraction of a codebase that is redundant. SlopCodeBench's operationalisation:
duplicated-or-redundant code as a share of total. OXN emits
`verbosity = duplicated_tokens / total_tokens` at file, component and repo scope, computed directly
from §3A.1's clone classes (counting each clone class's tokens once as original, the rest as
duplicate).

Two supporting measures worth emitting alongside it, because they are what actually moves during
agent sessions: **LOC growth rate** per checkpoint or commit, and **single-use helper count**
(functions called from exactly one site, defined in the same file, added in the same change) — the
latter is also the anti-gaming counter-metric of §10.5.

**Exactness.** EXACT given the clone classes. **Verdict: SELF-IMPLEMENT** (a ratio over §3A.1).
**Effort/Risk:** S (0.5 d) / Low.

### 3A.3 Structural erosion

**Definition.** The share of a codebase's total complexity mass concentrated in its most complex
functions — SlopCodeBench's second trajectory signal. Concretely, with functions sorted by cognitive
complexity descending:

- `erosion@k = Σ(complexity of top-k% functions) / Σ(complexity of all functions)` (default k = 10);
- and a scale-free variant, the **Gini coefficient** of the complexity distribution,
  `G = (2·Σ i·xᵢ) / (n·Σ xᵢ) − (n+1)/n` for values `x` sorted ascending.

Both are reported. The Gini form is preferred for cross-project comparison because `erosion@k`
depends on function count; `erosion@k` is preferred for a single project's trend line because it is
readable ("62% of your complexity lives in 10% of your functions").

**Why this and not a mean.** Software metric distributions are heavy-tailed (Louridas, Spinellis &
Vlachos, "Power laws in software," *TOSEM* 18(1), 2008), so mean complexity is uninformative and
barely moves as a codebase degrades. Concentration measures move. This is the same argument that
drives the risk-profile aggregation in §10.2, applied to a trend signal.

**Representation.** All functions' cognitive complexity — Tier 1, no resolution needed.

**Exactness.** EXACT. **Build-vs-adopt.** No free tool computes this; SlopCodeBench computes it
inside its own harness. **Verdict: SELF-IMPLEMENT** (~30 LOC over metrics we already have).
**Effort/Risk:** S (0.5 d) / Low.

**Gate usage for the whole tier.** *This paragraph said "duplication has a hard ceiling and can
block" from the start, and it never did.* `MAX_DUPLICATION_RATIO = 0.05` sat in
`thresholds.py` with no reader anywhere in the project — a promised gate that does not exist,
the same defect as `--deep`'s advertised cycle check. The constant is gone as of 2026-09-10,
and the evidence does not support switching it on either: Rahman, Bird & Devanbu (MSR 2010)
found, on four systems, that clones "may be **less** defect prone than non-cloned code". The
positive result — Juergens et al. (ICSE 2009), 107 developer-confirmed faults — is about
clones changed **inconsistently**, which needs the commit history and not a static ratio.
Duplication is therefore **report-only**, and the gateable form of it, if OXN ever wants one,
is a clone group whose members diverged in one commit. Verbosity and
erosion are **trend gates**, not absolute ones — they block only on a *worsening delta* within a
change, never on an absolute value, because an absolute erosion threshold on a legacy repo fails on
the first commit and gets the tool disabled (§10.3, Mode B).

---

## 4. Tier 2 — module and import graph, exact

### 4.1 Import extraction

**Representation.** Per-file CST → `IMPORTS` edges.

| Language | Nodes | Notes |
|---|---|---|
| Python | `import_statement` (`dotted_name`, `aliased_import`), `import_from_statement` (`relative_import` + `import_prefix` dot count, `wildcard_import`) | dynamic: `importlib.import_module(...)`, `__import__` → `kind=dynamic`, `resolved=false` |
| TS/JS | `import_statement` (`source`), `export_statement` with `source` (re-export), `call_expression` with callee `import` (dynamic), `require(...)`, `import_require_clause` | `import type` / `export type` → **`kind=type_only`** — no runtime coupling, which matters for Martin metrics and layering rules |
| Go | `import_declaration` → `import_spec` (string path, optional name) | `_` alias → `kind=side_effect`; `.` alias → dot-import |
| Rust | `use_declaration` (`scoped_use_list`, `use_wildcard`, `use_as_clause`), `extern_crate_declaration`; `mod_item` shapes the module tree but is **not itself an import edge** — see below | `crate::`/`super::`/`self::` resolved against the `mod` tree: 451 of 451 placed on ripgrep |
| Java | `import_declaration` (`scoped_identifier`, `asterisk`, `static`), `package_declaration` | package = directory path |

**Specifier → module resolution** — the largest bug source in the whole catalogue:
- **Python:** package roots from `pyproject.toml`/`setup.cfg`/`src` layout; relative imports resolved
  against the file's package; `from x import y` probes `x/y.py` and `x/y/__init__.py` to decide
  module-vs-name.
- **TS:** `tsconfig.json` `baseUrl`/`paths`; Node resolution (`index.ts`, `.ts/.tsx/.d.ts/.js`);
  `package.json` `exports`/`imports` (`#alias`); workspace globs.
- **Go:** `go.mod` `module` line — internal iff the import path carries that prefix.
- **Rust:** `Cargo.toml` plus the `mod` tree.
- **Java:** source roots plus package prefix.

**Non-negotiable design rule:** unresolved specifiers are **never silently dropped**.
`oxn check --json` emits `unresolved_imports: [...]`, and `oxn.yaml` accepts `module_aliases` for
manual repair. Silent dropping turns a layer-violation gate into a false-negative machine.

**Exactness.** EXACT for static imports; dynamic imports and re-export chains are APPROX and flagged.
**Rust's `mod b;` declares containment, not dependency, and carries no edge.** It is what pulls
`b.rs` into the crate, so the argument for an edge is real — but the graph is about coupling, and a
parent module listing its children is the Rust spelling of a directory listing. Nothing is lost by
omitting it: intra-crate paths resolve *against* the module tree without being edges in it, and on
ripgrep every one of 451 `crate::`/`super::`/`self::` specifiers is placed, with the 394 unplaced
being external crates. The one shape that under-reports is a file whose only outgoing reference is a
`mod` declaration — a bare `lib.rs` — which has no outgoing edges at all. Measured rather than
assumed, and stated here because the row above listed `mod_item` as an import kind for a while when
it is not one.

**Build-vs-adopt.** Free: **grimp/import-linter** (BSD-2, Python, Rust core, very good); **pydeps**
(BSD); **dependency-cruiser** (MIT, JS/TS, excellent — also cycles and DSM); **madge** (MIT);
**jdeps** (JDK, bytecode); **tach** (MIT, Python boundaries).
**Verdict: SELF-IMPLEMENT (HYBRID).** Each free tool is single-ecosystem and a subprocess; we need
one uniform `IMPORTS` model feeding Martin/Lakos/DSM/Arcan across five languages. grimp,
dependency-cruiser and jdeps are **differential oracles for graph equality** — a very strong test.
**Effort/Risk:** M (6–10 d, mostly per-language config parsing) / **High**.

### 4.2 Module dependency graph and Tarjan SCC

**Definition.** Tarjan, "Depth-first search and linear graph algorithms," *SIAM J. Computing* 1(2),
1972. O(V+E).

**Recipe.** Aggregate `IMPORTS` to `DEPENDS_ON` at the configured granularity (file / module /
package / declared component). **Implement Tarjan iteratively** with an explicit stack — Python's
default 1000-frame recursion limit *will* be hit on real dependency chains, and the crash is
non-obvious. Produce the SCC list, node→SCC map, and the **condensation DAG**, which every downstream
algorithm consumes.

**Exactness.** EXACT given the graph. **Verdict: SELF-IMPLEMENT (HYBRID)** — iterative Tarjan is ~60
LOC, and the measured **146 ms `import networkx`** is 70%+ of the entire hook budget for a dependency
we would use for five functions. networkx is the **test oracle**, asserted equal on random graphs.
**Effort/Risk:** S (1 d) / Low.

### 4.3 Martin metrics — Ca, Ce, I, A, D

**Definition.** Robert C. Martin, "OO Design Quality Metrics: An Analysis of Dependencies" (1994);
*Agile Software Development: Principles, Patterns, and Practices* (2002), ch. 20.

- **Ce (efferent)** = number of *external* components this component depends on.
- **Ca (afferent)** = number of external components depending on it.
- **Instability** `I = Ce / (Ca + Ce)` ∈ [0,1]; `I = 0` when Ca = Ce = 0 (OXN convention, documented).
- **Abstractness** `A = Na / Nc` (abstract types ÷ total types) ∈ [0,1].
- **Distance from the main sequence** `D = |A + I − 1|`. Martin's original is `|A+I−1|/√2`; the
  normalised form is what everyone uses — OXN emits `D_norm` and says so.

**Abstractness without a type system — per-language operationalisation:**

| Language | A type counts as ABSTRACT if… |
|---|---|
| **Python** | inherits `abc.ABC` or has `ABCMeta` metaclass; **or** any method decorated `@abstractmethod`/`@abstractproperty`; **or** inherits `typing.Protocol`; **or** every method body is `pass` / `...` / `raise NotImplementedError`. `TypeVar`, `Protocol` and `TypedDict` definitions count as abstract declarations. |
| **TS/JS** | `interface_declaration`; `abstract_class_declaration` (verified: the TS grammar emits a **distinct node kind**, not `class_declaration` with a modifier); `type_alias_declaration` (config-gated, default yes); ambient `declare` decls; **every type in a `.d.ts` file**. |
| **Go** | `type_spec` whose type is `interface_type`. Structs are concrete. |
| **Rust** | `trait_item`. `struct_item`, `enum_item`, `impl_item` are concrete. |
| **Java** | `interface_declaration`; `class_declaration` with `abstract`; `annotation_type_declaration` (config-gated); `record_declaration` is concrete. |

**Edge case — components with zero types**, very common in Go, Rust and functional Python:
`Nc = 0` → `A` undefined. OXN policy: report `A = null`, exclude the component from D-based gates,
and additionally emit a **functional abstractness proxy** = exported interfaces/traits/protocols/type
aliases ÷ all exported symbols, flagged APPROX.

**Exactness.** Ca/Ce/I: **EXACT** given the module graph. A: **EXACT syntactically, APPROXIMATE
conceptually** — Python duck typing means a fully concrete class can serve as an interface, and Go's
implicit interface satisfaction means a struct can be the abstraction at a call site. Documented, not
hidden.

**Build-vs-adopt.** Free tools computing Martin metrics polyglot: essentially none — Sonargraph,
Structure101 and NDepend all cost money, so the cost rule forces self-implementation.
**Verdict: SELF-IMPLEMENT.** **Effort/Risk:** S–M (2–3 d) / Medium — abstractness rules are the risk.

### 4.4 Lakos levelization — CCD / ACD / NCCD

**Definition.** John Lakos, *Large-Scale C++ Software Design*, Addison-Wesley, 1996, §4.

- **Level:** `level(c) = 0` if c has no dependencies, else `1 + max(level(d))`. Requires acyclicity,
  so compute on the **condensation DAG**; a cyclic component's level is its SCC's level.
- **CCD** `= Σ_c |DependsOn*(c)|`, where `DependsOn*` is the **reflexive** transitive closure.
- **ACD** `= CCD / N`. **NCCD** `= CCD / CCD_btree(N)` with `CCD_btree(N) = (N+1)·log₂(N+1) − N`.
- Interpretation: `NCCD > 1` means more coupled than a balanced binary tree; `NCCD ≫ 1` means cycles
  or a big ball of mud.

**Recipe.** SCC → condensation → reverse-topological order → **bitset transitive closure** (§7).
Levelization is a longest-path DAG computation in the same pass.

**Exactness.** EXACT. Free polyglot options: none (Lattix and Structure101 are paid).
**Verdict: SELF-IMPLEMENT.** **Effort/Risk:** S (1–2 d) / Low.

### 4.5 Propagation Cost and the Design Structure Matrix

**Definition.** MacCormack, Rusnak & Baldwin, "Exploring the Structure of Complex Software Designs,"
*Management Science* 52(7):1015–1030, 2006. DSM lineage: Steward (1981); Baldwin & Clark, *Design
Rules* (2000). Core/periphery classification: Baldwin, MacCormack & Rusnak (2014).

- Binary N×N dependency matrix **M**; visibility matrix **V** = boolean `Σ_{n=0..N} Mⁿ`
  (reflexive-transitive closure).
- **Propagation Cost** `= (Σᵢⱼ Vᵢⱼ) / N²` ∈ (0, 1].
- Per-node **VFI** (visibility fan-in) = column sum, **VFO** = row sum → core/periphery
  classification (Core = high VFI *and* high VFO).

**Recipe (performance-critical).** Naïve Warshall is O(N³) and infeasible in Python at N = 5000.
Instead: SCC-condense, then in **reverse topological order** compute
`closure(v) = bit(v) | ⋃_{w ∈ succ(v)} closure(w)` using **Python arbitrary-precision ints as
bitsets**. Cost is one big-int OR per edge, each O(N/64) words — for N = 5000, E = 20000 that is
~1.5 M word ops, i.e. milliseconds. Expand SCC members at the end.

**Exactness.** EXACT. dependency-cruiser (MIT) emits a DSM for JS/TS only; commercial tools own this
space. **Verdict: SELF-IMPLEMENT.** **Effort/Risk:** S (1–2 d) / Low.

### 4.6 Modularity Q

**Definition.** Newman & Girvan, "Finding and evaluating community structure in networks,"
*Phys. Rev. E* 69, 026113 (2004): `Q = (1/2m) Σ_ij [A_ij − k_i·k_j/(2m)]·δ(c_i, c_j)`. Directed
variant: Leicht & Newman, *PRL* 100, 118703 (2008).

**Scoping decision.** OXN evaluates Q **on the partition that already exists** — packages,
directories, declared layers. This is a *measure* (exact, O(E), ~30 LOC), not community detection.
It answers: is the declared modularisation consistent with the actual dependency structure?

Community detection (Louvain — Blondel et al. 2008; Leiden — Traag et al. 2019) is deferred to the
research track, where "discovered modules vs declared modules" becomes an architecture-erosion signal
for the paper.

**Exactness.** EXACT. **Verdict: SELF-IMPLEMENT Q** (30 LOC, hot path), networkx as oracle;
**ADOPT networkx (BSD-3) for Louvain in the research/eval harness only**, where it is not on the hook
path. **Effort/Risk:** S (0.5 d) / Low.

### 4.7 Layer-invariant violations

**Definition.** The right model is **Reflexion Models** — Murphy, Notkin & Sullivan, "Software
reflexion models," *FSE 1995* / *TSE 2001*: compare a high-level model against the extracted source
model and classify edges as **convergent / divergent / absent**. That framing is strictly better than
ad-hoc "forbidden imports" and should be OXN's model.

Config shape, with contract vocabulary borrowed from import-linter, which got it right:

```yaml
layers:
  domain:         ["src/domain/**"]
  application:    ["src/application/**"]
  infrastructure: ["src/infrastructure/**"]
contracts:
  - type: layered            # ordered; higher may import lower, never the reverse
    order: [infrastructure, application, domain]
  - type: forbidden
    source: domain
    forbidden: [infrastructure, "fastapi", "sqlalchemy"]
  - type: independence
    modules: [billing, shipping]
  - type: deep_import        # may only enter package X via X.ports
    package: domain
    allowed_entrypoints: ["domain.ports.*"]
```

Detection: for each `DEPENDS_ON` edge, evaluate the contract predicates. Report the **shortest
violating import chain** (BFS on the module graph), not just the endpoints — that is what makes the
diagnostic actionable for an agent.

**Exactness.** EXACT given the module graph, which is the weak link (§4.1).
**Build-vs-adopt.** Free: **import-linter** (BSD-2, Python), **tach** (MIT, Python),
**dependency-cruiser** (MIT, JS/TS), **ArchUnit** (Apache-2.0, JVM test library), **Nx boundaries**
(MIT). All single-ecosystem; none integrates with ADR-derived constraints.
**Verdict: SELF-IMPLEMENT.** This is OXN's differentiating feature — ADRs emit contracts — so it must
be one uniform engine and the contracts must be *data* (§8). import-linter and dependency-cruiser are
oracles on their native ecosystems. **Effort/Risk:** M (3–5 d) / Medium.

### 4.8 Arcan's four architectural smells

**Citations.** Fontana, Pigazzini, Roveda, Tamburri, Zanoni & Di Nitto, "Arcan: A Tool for
Architectural Smells Detection," *ICSA-C 2017*; Fontana, Roveda, Zanoni et al., "Automatic Detection
of Instability Architectural Smells," *ICSME 2016*; Suryanarayana, Samarthyam & Sharma, *Refactoring
for Software Design Smells*, Morgan Kaufmann, 2014; cycle shapes from Al-Mutawa, Dietrich, Marsland &
McCartin, "On the Shape of Circular Dependencies in Java Programs," *ASWEC 2014*.

| Smell | Detection rule | Literature threshold | OXN default |
|---|---|---|---|
| **Cyclic Dependency (CD)** | SCC with size > 1, at both unit (class/file) and container (package) level | any cycle is a smell; severity ∝ size and edge density | Tarjan; report SCC size, edge density and **shape** ∈ {tiny (2 nodes), circle (all degrees 1), clique, star (single articulation hub), chain, mixed} |
| **Hub-Like Dependency (HL)** | High fan-in **and** fan-out. Arcan 2.x: `TotalDeps(x) = FanIn + FanOut`; `Threshold = max(Threshold_System, Threshold_Benchmark)` from frequency analysis over the system and a 100+-system benchmark (Qualitas Corpus) | system-adaptive; no fixed number | `TotalDeps > p90(project)` **AND** balance guard `min/max ≥ 0.5` **AND** `min(FanIn,FanOut) ≥ 5`. Marked as **OXN's operationalisation**, configurable |
| **Unstable Dependency (UD)** | Container x depends on containers *less stable* than itself: `I(x) > I(y) + δ` for `y ∈ deps(x)` — violates Martin's SDP | Degree of Unstable Dependency **DoUD ≥ 30%** (Fontana et al., ICSME 2016; chosen by manual validation) | `δ = 0.0`, `DoUD ≥ 0.30` |
| **God Component (GC)** | Container size (LOC or class count) far above the norm | Lippert & Rook (*Refactoring in Large Software Projects*, Wiley 2006) fixed **27 000 LOC**; Arcan prefers adaptive | `max(p90(project_component_LOC), 5000)`, configurable; 27 000 offered as `preset: lippert_rook` |

**Representation.** Component-level `DEPENDS_ON` graph, component LOC, and Martin I. All Tier 2 — no
resolution needed.
**Exactness.** EXACT given the graph *and a stated threshold policy*. The thresholds themselves are a
policy choice, never "exact" — surface which policy produced each finding.
**Build-vs-adopt.** Arcan is now a commercial product (arcan.tech), so the cost rule forces
self-implementation; Designite's free tier is limited and Sonargraph/Structure101 are paid.
**Verdict: SELF-IMPLEMENT.** **Effort/Risk:** M (4–6 d) / Medium — algorithms are trivial once
§4.2/§4.3 exist; the risk is entirely threshold calibration (§10).

---

## 5. Tier 2.5 — VCS metrics: exact, zero dependencies, best value-to-effort ratio in the catalogue

**Extraction — a single streamed call, parsed incrementally:**

```
git -c core.quotepath=false log --no-merges --numstat --date=iso-strict \
    -M -C --find-renames=50% \
    --pretty=format:'§%H|%an|%aE|%ad|%s' -- <paths>
```

**Parsing edge cases, all real:**
- Rename syntax appears in two forms under `--numstat`: `old/path => new/path` and the brace form
  `dir/{a => b}/file.py`. Normalise both, and build a **rename chain map** so each file's history is
  continuous under its *current* path.
- Binary files emit `-\t-\tpath`.
- `--no-merges` by default (merge commits double-count); offer `--first-parent` for release branches.
- `.mailmap` normalisation for author identity, or ownership metrics are garbage.
- `core.quotepath=false` for non-ASCII paths.
- Configurable `exclude_paths` (vendored, generated, lockfiles) and `exclude_commits` (bulk
  reformats, licence headers). Without these, one `black`/`prettier` run destroys every churn signal.

### 5.1 Code churn
Nagappan & Ball, "Use of relative code churn measures to predict system defect density," *ICSE 2005*.
Absolute churn = Σ(added + deleted), but the **relative** measures (churn/LOC, churn/file-count,
files-churned/files-total) were the predictive ones — emit those, not just absolutes. Also: commit
count, distinct author count, and code age (time since last modification — Tornhill).
**Exactness:** EXACT w.r.t. recorded history. **Effort:** S (1 d) / Low.

### 5.2 Change coupling / logical coupling
Gall, Hajek & Jazayeri, *ICSM 1998*; Ball, Kim, Porter & Siy, *ICSE '97 Workshop*; Zimmermann,
Weißgerber, Diehl & Zeller, *TSE* 31(6), 2005; Tornhill, *Your Code as a Crime Scene* (2015) and
*Software Design X-Rays* (2018).

For a pair (a,b): `support = |commits containing both|`;
`confidence(a→b) = support / |commits containing a|` (**asymmetric**);
`jaccard = support / |commits containing a or b|`.
**Filters that make or break the signal:** `min_support ≥ 5`, and **exclude commits touching more
than K files** (K ≈ 20–50) — squashed and mechanical commits couple everything to everything.
**Sum-of-coupling(a)** = Σ support over partners, identifying the change magnets.

### 5.3 Temporal coupling across a commit window
Some teams split one logical change across several commits. Group commits into **change sets** by
`(author, time window ≤ T (default 5 min), message similarity)` before computing coupling.
Configurable, off by default; report both.

### 5.4 Hotspots
Tornhill (2015/2018): `hotspot = normalize(change_frequency) × normalize(complexity)`. The complexity
term can be SLOC, indentation-based complexity (Hindle, Godfrey & Holt, *ICPC 2008*), or — OXN's
default — **cognitive complexity**, a strictly better proxy that we already compute. Rank by product;
emit the top N with both axes visible so the user sees *why*.
**Use:** prioritisation only. A hotspot never blocks a commit (§10).

### 5.5 Knowledge and ownership maps
Bird, Nagappan, Murphy, Gall & Devanbu, "Don't touch my code! Examining the effects of ownership on
software quality," *FSE 2011*. `ownership(e) = max_a(commits_a(e) / commits(e))`; **minor
contributors** = authors with < 5% of commits, **major** = ≥ 5%. Bird et al. found the *number of
minor contributors* the strongest defect correlate. Derived: bus factor / knowledge-loss map
(fraction of code whose top owner has been inactive > N months), and an author-per-component matrix
as a Conway's-law check against the declared structure.

**Exactness (all of §5).** EXACT with respect to recorded history — with the standing caveat that
squash-merges, monorepo migrations and vendoring distort ground truth. Report the commit count and
date range used, so the numbers are interpretable.

**Build-vs-adopt.** Free: **code-maat** (Tornhill; **GPL-3.0**, JVM — cannot be a runtime dep of an
MIT wheel, but *is* licence-clean as a CI-only subprocess oracle, since there is no distribution);
**PyDriller** (Apache-2.0, pulls GitPython); **git-of-theseus**; **hercules** (MIT, Go).
**Verdict: SELF-IMPLEMENT (HYBRID).** ~400 LOC over one `git log` subprocess: zero dependencies, no
JVM, no GitPython. code-maat and PyDriller are the differential oracles.
**Effort/Risk:** S–M (3–4 d) / Low. **This tier needs no parser, no resolver and no grammar work, so
it ships early** — see the roadmap phase ordering.

---

## 6. Tier 3 — requires name and type resolution

### 6.1 LCOM family

**Definitions.**
- **LCOM1** — Chidamber & Kemerer, *OOPSLA 1991*: number of method pairs sharing **no** instance
  variable.
- **LCOM2** — CK, "A Metrics Suite for Object Oriented Design," *IEEE TSE* 20(6), 1994:
  `max(0, |P| − |Q|)`, P = non-sharing pairs, Q = sharing pairs.
- **LCOM3** — Li & Henry, "Object-oriented metrics that predict maintainability," *JSS* 23(2), 1993:
  number of connected components of G(methods, edges = shared attribute access).

**Inheritance is read per grammar, and `LanguageProfile.supertype_fields` names where.** Python has
`superclasses`, Java `superclass` *and* `interfaces`, Rust the `trait` an `impl` satisfies, and
TypeScript none at all — it hangs a `class_heritage` child off the declaration. Reading only Python's
left DIT and NOC at zero for four of six languages, which is invisible because 0 is what a root class
reports and most classes are roots. Go declares no inheritance: its interfaces are satisfied
structurally, so there is no edge and reporting one would be an invention. Descent into a heritage
clause goes through wrapper kinds only, which is what stops `extends Box<Inner>` yielding `Inner`;
`type_parameters` is not a supertype field, and reading it made `class Foo[T](Base)` report `T` as an
ancestor. The hierarchy table is keyed by language, because a bare supertype name means one thing
inside a language and nothing across two.
- **LCOM4** — Hitz & Montazeri, "Measuring coupling and cohesion in object-oriented systems," 1995:
  LCOM3 **plus** edges for intra-class method invocation. Also defines **connectivity**
  `C = 2(|E| − (n−1)) / ((n−1)(n−2))`, used to discriminate classes when LCOM4 = 1.
- **LCOM\* / LCOM5** — Henderson-Sellers, *Object-Oriented Metrics: Measures of Complexity*,
  Prentice Hall, 1996:
  `LCOM* = ( (1/a)·Σ_{j=1..a} μ(A_j) − m ) / (1 − m)`, a = #attributes, m = #methods,
  μ(A_j) = #methods accessing attribute A_j. Range ≈ [0,1]; undefined for m = 1 or a = 0.

**Representation.** Class member graph: methods, fields, `READS`/`WRITES` edges, intra-class `CALLS`.

**CST recipe — field access, the crux.**
- **Python:** `attribute` node whose `object` is an `identifier` equal to the method's **actual first
  parameter name** — read it from the CST, never hardcode `"self"`. Skip `@staticmethod`; for
  `@classmethod` the receiver is the class. *Implemented*, `ScopeSpec.static_markers`; the
  `@staticmethod` exclusion is held by `test_a_python_static_method_has_no_receiver`, and without
  it `parse(raw)` made `raw` the receiver and every `raw.strip()` a field access on the class. The field set is the union of `self.x = …` assignments
  across all methods (especially `__init__`), class-level assignments, `__slots__`, and
  dataclass/annotation fields.
- **TS/JS:** `member_expression` with `object = this`; `#private` fields; and TS constructor parameter
  properties (`constructor(private x: T)`) which *define* fields.
- **Java:** `field_access` with `this`, **plus bare identifiers that resolve to fields** — this needs
  L0 scope resolution to exclude locals and parameters that shadow the field. Without it, LCOM is
  simply wrong. *Implemented*, `ScopeSpec.bare_field_access`. It is not optional in practice: Spring
  style writes `vets.findAll()`, and reading only the qualified form put petclinic's
  `VetControllerTests` at LCOM\* 1.125 — above 1, this document's own signal for "the fields are
  never touched" — while stamping it EXACT. With bare access it reads 0.875 and LCOM4 5 → 2. The
  shadowing controls are in `tests/fixtures/cohesion_shapes/bare_fields.java.txt`: a constructor
  parameter and a local, both named for a field, neither counted. A declaration's own name is also
  excluded, without which `void save(...)` recorded the method as accessing itself.
- **Go:** methods are `method_declaration` with a receiver; the "class" is the receiver's named type
  (**impl spread across files → needs L1**); field access is `selector_expression` on the receiver.
  The receiver is written outside the parameter list, so it reaches no parameter scan and is read
  from the `receiver` field — both its type (`receiver_type`) and its variable name.
- **Rust:** the "class" is a type plus **all its `impl` blocks**, possibly in several files (L1);
  field access is `field_expression` on `self`. `&self` is a `self_parameter` node rather than a
  parameter, and its presence is the only thing separating a method from an associated function:
  `fn save(&self)` and `fn new(cfg: Config)` sit side by side in one `impl`.

Both are joined **within a file** today; a type whose methods are declared in a sibling file keeps
only the ones written beside it. `LanguageProfile.implements_field` names the type an `impl` block
belongs to, and generic arguments are dropped so `impl<'b, R> LineBufferReader<'b, R>` and `struct
LineBufferReader` are one type rather than two models with eight methods and none.

**What breaks without resolution, precisely:**
1. **Inherited fields are invisible** → a subclass touching only inherited state looks maximally
   incohesive. Needs L1 for in-repo bases, L2 for external ones.
2. **Implicit `this`** (Java, C#, C++) is indistinguishable from a local without L0 scope resolution.
3. **Dynamic attributes** (`setattr`, `__dict__`, `Object.assign`, metaprogramming) are invisible at
   *any* resolution level. Permanently APPROX.
4. **Properties and accessors** hide field access behind a method call — `LCOM4`, which adds call
   edges, is much more robust here than LCOM1/2.
5. **A constructor that assigns every field** shares a field with every method and so joins every
   component through itself. This one is not a resolution limit — it is what LCOM3 and LCOM4 say as
   published, and it is a false negative on the shape they exist to find. Measured against a matched
   control pair (`tests/fixtures/cohesion_shapes`, a wide repository and a five-collaborator God
   Class at the same method count), the God Class reported **one** component, the identical answer
   to the repository. **OXN excludes constructors from the component scan** — Python `__init__` and
   `__new__`, TS/JS `constructor`, Java's method named for its class; Go has none, and Rust's `new`
   is a convention rather than a constructor, so it stays in. Excluded, the God Class reports five
   components and they *are* its five collaborators. The exclusion is shared by LCOM3 and LCOM4
   rather than taken by LCOM4 alone, because edges can only merge components and `LCOM4 ≤ LCOM3`
   must hold. LCOM1, LCOM2 and LCOM\* are ratios over pairs rather than a partition; the argument
   does not transfer and they still count constructors.

**Exactness.** **EXACT** for explicitly-qualified field access at L0; **EXACT in-repo** at L1
(inheritance); never exact under metaprogramming.

**OXN recommendation:** headline **LCOM\*** (normalised, language-neutral, comparable) and **LCOM4**
(component count — the actionable one: "this class splits cleanly into 3 pieces"). Emit LCOM1–3 for
compatibility. **Publish the exact formula used** — LCOM is the most misreported metric in the
industry.

**Build-vs-adopt.** Free polyglot LCOM tools: essentially none. `ck` by Aniche (Apache-2.0) is a good
Java-only oracle; Designite, NDepend and Understand are paid. **Verdict: SELF-IMPLEMENT**, with `ck`
as the Java oracle. **Effort/Risk:** M (4–6 d) / **Medium-High** — definition variance plus receiver
identification.

### 6.2 CK suite

Chidamber & Kemerer, *IEEE TSE* 20(6), 1994.

| Metric | Definition | Needs | Exactness | Notes |
|---|---|---|---|---|
| **WMC** | Σ complexity(mᵢ); unit weights ⇒ method count (NOM) | CST only | **EXACT** | OXN default weight = cyclomatic; also emit `WMC_cognitive` and `NOM` |
| **DIT** | longest path to the inheritance-tree root | superclass resolution | L1: **EXACT in-repo**; external bases counted as `depth+1` with `dit_external_unresolved: true`. L2: **EXACT** | `class X(BaseModel)` — pydantic's own depth is unknowable without L2 |
| **NOC** | number of immediate subclasses | subclass resolution | L1: **EXACT in-repo** | multiple inheritance and mixins are fine; open-world subclasses are never countable |
| **CBO** | number of classes this class is coupled to | **type resolution of receivers** | L0/L1: **APPROX** (name heuristic scoped by imports — over-counts on name collision, under-counts on aliasing and duck typing). L2: **EXACT over resolved symbols** | still a static approximation of runtime coupling |
| **RFC** | \|M ∪ ⋃_{m∈M} R_m\|, R_m = methods called by m | **call graph** | L0/L1: emit `RFC_syntactic` = distinct call-site names, documented APPROX. L2: **EXACT-static** | virtual dispatch resolves to declared targets only |

**Build-vs-adopt.** Free: `ck` (Apache-2.0, Java only); rust-code-analysis has no CK; **CodeQL is free
only for public repos and is therefore excluded by the cost rule for private use**. Paid: Understand,
NDepend, Designite. **Verdict: SELF-IMPLEMENT** on the OCG, with `ck` as the Java oracle.
**Effort/Risk:** M (4–5 d given L1/L2) / Medium.

### 6.3 Call-graph construction

- **L0/L1 (name-based):** a call site maps to any repo-defined function or method whose name matches,
  filtered by the file's import visibility and receiver-type hints. Emit `confidence = 1/|candidates|`.
  Common names (`get`, `run`, `handle`) blow up the candidate set — which is exactly why it is APPROX.
- **L2 (SCIP):** the range-join recipe of §2.2, recording `dispatch = static | virtual | dynamic`.
- **Never resolvable:** higher-order functions, callbacks, reflection, DI containers, `eval`,
  monkey-patching, dynamic imports.

**Uses.** The cognitive-complexity recursion increment (SCC on the call graph), RFC, function-level
fan-in/out, dead code.
**Build-vs-adopt.** **Joern** (Apache-2.0) builds a real CPG but needs a JVM and minutes → oracle and
research track only. `pycg` (Apache-2.0, Python), `jelly`/`js-callgraph` (JS). **Verdict:
SELF-IMPLEMENT on top of L2**; Joern and pycg as oracles. **Effort/Risk:** M (3–5 d given L2) / Medium.

### 6.4 Dead code

Reachability from declared roots on the OCG: `__main__` guards, `main()` (Go/Rust), exported public
API, test files, CLI entrypoints, and framework entrypoints declared in `oxn.yaml` (routes, DI
registrations, plugin manifests). Unreached definitions are **candidates**.

**Exactness: never exact.** Always reported as candidates, with the root set used and a confidence
tier. Reflection and DI make false positives inevitable; a gate that deletes code on this signal is
dangerous.

**Measured false-positive sources, because "candidates" is not a licence to leave them
unquantified.** As first shipped, this reported **138 candidates on `python-httpx` and every one of
them was wrong.** Three causes, all fixed on 2026-09-10, and one that remains:

* *Members the language dispatches without naming.* 124 of the 138. `BasicAuth(...)` names the
  **class**, so a constructor has no inbound call edge, and `==` and `for` reach `__eq__` and
  `__iter__` with no call site at all. The other 14 were private helpers those members were the only
  callers of. Modelled by making a class a **node** in the traversal — not by rooting dispatched
  members, which would hide a private class nobody constructs.
* *Calls written at module scope.* No enclosing definition, so the edge was dropped and every
  `_main` under `if __name__ == "__main__"` was reported dead. Worth 5,718 edges on OXN's own tree:
  call coverage 92.64% → 98.75%.
* *Privacy assumed to be Python's.* `leaf.startswith("_")` is Python's rule and Go's by coincidence.
  Java, Rust and TypeScript spell privacy with a modifier, so every callable was rooted and the
  total was **0 by construction**. Now routed through `LanguageProfile.is_private`, and a tree where
  nothing can be judged private is answered UNAVAILABLE rather than zero.
* *Still open: a callable referenced as a value.* A dispatch-table entry, a callback, a sort key —
  no call node exists, so nothing reaches it and everything downstream of it is reported. On OXN's
  own `src/`: 69 candidates, 38 of them roots of the dead forest, and **36 of those 38 are a
  reference rather than a call**. One shape, one relation: `EdgeKind.REFERENCES` is declared and
  nothing populates it.

**"Public is a root" is a library's assumption and deliberately the safe one.** A public method with
no in-tree caller may be the API someone else imports, so it is never reported. On an application
that is a false-negative machine, which is why a zero here is not a clean bill of health.
**Build-vs-adopt.** Free: **vulture** (MIT, Python), **knip**/**ts-prune** (MIT, TS),
`golang.org/x/tools/cmd/deadcode` (BSD). **HYBRID** — self-implement the uniform reachability pass on
the OCG (one model, five languages); vulture and knip are oracles.
**Effort/Risk:** S–M (2–3 d) / **High** (false positives).

### 6.5 Function-level fan-in / fan-out

Henry & Kafura, "Software Structure Metrics Based on Information Flow," *IEEE TSE* SE-7(5), 1981:
`complexity = length × (fan-in × fan-out)²`, with fan-in/out defined via **information flow**
(parameters, globals, return values), not merely calls.

**Honest note:** the ubiquitous "count the calls" operationalisation is a simplification, criticised
in Shepperd's work on metric validity (Shepperd, "A critique of cyclomatic complexity as a software
metric," *Software Engineering Journal*, 1988; Shepperd & Ince on information-flow metrics). OXN uses
the call-graph operationalisation and **says so**. Exactness follows the call graph: APPROX at L0/L1,
EXACT-static at L2. **Effort:** S (1 d) / Low, given the call graph.

---

## 7. Self-implemented algorithm inventory

Used on the hot path, ~300 LOC total, in `oxn/graph/algos.py`, with **networkx (BSD-3) as the test
oracle only** — the measured 146 ms import cost makes networkx unaffordable in a 200 ms hook budget.

| Algorithm | Use | Notes |
|---|---|---|
| **Iterative Tarjan SCC** | cycles, condensation, recursion cycles | must be iterative — Python's 1000-frame recursion limit *will* be hit |
| **Condensation DAG** | Lakos, DSM, layering | |
| **Kahn topological sort** | levelization, closure order | detects cycles as a byproduct |
| **Bitset transitive closure** (reverse-topo over the condensation, Python ints as bitsets) | CCD, propagation cost, VFI/VFO, reachability | O(E · N/64) — milliseconds at N = 5000 |
| **Longest-path DAG levelization** | Lakos levels | |
| **BFS shortest violating path** | actionable layer diagnostics | |
| **Newman Q on a given partition** | modularity | ~30 LOC |
| **Connected components (undirected)** | LCOM3 / LCOM4 | union-find |
| **Interval tree / sorted-range bisect** | SCIP occurrence ↔ CST range join | |
| **Semi-naive Datalog with stratified negation** | the rule engine (§8) | later phase |

---

## 8. The symbolic reasoning layer

| Option | Licence / weight | Expressiveness | Latency | Verdict |
|---|---|---|---|---|
| **(a) Hand-rolled Python graph predicates** | 0 deps | Low — each rule is code; recursion hand-written per rule; rules not user-authorable | µs–ms | **MVP primary** |
| **(b) In-house Datalog: semi-naive evaluation + stratified negation** (~500–800 LOC) | 0 deps | High for our domain — recursion free (`reaches(X,Y) :- depends(X,Z), reaches(Z,Y).`), negation for "no import except via ports"; least-fixpoint semantics, fully deterministic | ms on 10²–10³-node graphs | **Later-phase primary** |
| **(c) clingo / ASP** | **MIT**, pip wheel ~20–40 MB | Highest — choice rules plus optimisation, giving *"the minimal set of edges to remove to break all cycles"*, a genuinely valuable repair suggestion | grounding fine at our scale | **Optional extra `oxn[asp]`, research track** |
| **(d) Z3** | **MIT**, `z3-solver` wheel | Wrong tool for graph rules; right tool for state-machine and invariant verification | — | **Optional extra `oxn[smt]`** — this is `idea.md` §8's `verify_state_invariants` |

**Recommendation.**
- **MVP: (a).** Ten built-in rules, hand-written, zero dependency, microseconds. Uncontested.
- **Later phase: (b), in-house Datalog.** Justified under the *cost-based* rule, not purity: the
  **product requirement is rules-as-data**. ADRs must emit machine-checkable constraints, so the
  constraint language is part of OXN's user-facing contract and must be stable, embeddable,
  versionable, and dependency-free on the hook path. clingo is free and would work, but adds a
  20–40 MB wheel with platform-specific builds to a tool whose install promise is `pip install oxn`,
  and ASP's stable-model semantics is strictly more than the monotone reachability plus stratified
  negation we need — and much harder for users to debug. Prior art for Datalog-as-program-analysis:
  **Doop** (Bravenboer & Smaragdakis, *OOPSLA 2009*), **Soufflé** (Jordan, Scholz & Subotić,
  *CAV 2016*), and CodeQL's Datalog lineage. Semi-naive evaluation is textbook: Bancilhon &
  Ramakrishnan (1986); Abiteboul, Hull & Vianu, *Foundations of Databases* (1995), ch. 13.
- **(c) and (d) as optional extras**, both MIT, both free, for the repair-optimisation and
  state-invariant research extensions respectively.

**Why this matters for the paper:** "ADRs as the source of machine-checkable constraints" is only a
real contribution if the constraints are *data*, in a declarative language with defined semantics.
Datalog is that language.

---

## 9. Differential-testing strategy

Five test families. **Oracles are never runtime dependencies**; they run in a CI container carrying
node, go, JDK, cargo and a JVM.

### 9.1 Golden corpora — authoritative
Hand-scored snippets per language per metric. These, not the oracles, *define* correctness. Must
include every example from the Cognitive Complexity white paper v1.7 (`sumOfPrimes` = 7,
`getWords` = 1, the try/catch `myMethod` = 9, `myMethod2` = 2 and 4, `overriddenSymbolFrom` = 19,
`addVersion` = 35, `toRegexp` = 20, `model.js save` = 20), the boolean-sequence examples (+3 and +2),
the Python-decorator exception trio (1, 2, 1), the JS declarative-outer-function pair (1, 3), and
**`idea.md`'s own example scoring exactly 6**.

### 9.2 Differential vs free oracles — characterisation, not equality
One **authoritative oracle per metric per language**, plus a generated **divergence table**
(`docs/divergences.md`) asserting *known* differences. A suite expecting equality against tools that
disagree with each other is red forever.

| Metric / language | Authoritative oracle | Licence | Assertion |
|---|---|---|---|
| CC / Python | **radon** | MIT | equality after documented divergence rules |
| CC / all launch langs | **lizard** | MIT | equality per divergence table |
| Cognitive / Python | **complexipy** | MIT | equality |
| Cognitive / Go | **gocognit** | MIT | equality |
| Cognitive / JS-TS | **eslint-plugin-sonarjs** | **LGPL-3.0**, CI subprocess only | equality |
| Cognitive / Halstead / MI, multi-lang | **rust-code-analysis** | MPL-2.0 | cognitive: equality; Halstead: correlation |
| Halstead + MI / Python | **radon** | MIT | **Spearman ρ ≥ 0.75** only — radon counts a strictly narrower operator set (§3.6); this checks the metrics move together, nothing more |
| Param count, NLOC | **lizard** | MIT | equality |
| Import graph / Python | **grimp / import-linter** | BSD-2 | **graph-equality** (node and edge sets) |
| Import graph / JS-TS | **dependency-cruiser** | MIT | graph-equality + cycle-set equality |
| Import graph / Java | **jdeps** | JDK | graph-equality at package level |
| Graph algorithms | **networkx** | BSD-3 | equality on random graphs |
| VCS metrics | **code-maat** (GPL-3.0, CI subprocess = licence-clean), **PyDriller** (Apache-2.0) | | equality on churn and coupling |
| Call graph / dead code | **Joern** (Apache-2.0), **vulture** (MIT), **knip** (MIT) | | precision/recall, not equality |
| CK / Java | **ck** (Aniche) | Apache-2.0 | equality on WMC/DIT/NOC; correlation on CBO/RFC/LCOM |
| **Name resolution (L0/L1)** | **the SCIP indexers themselves** | Apache-2.0 | **precision/recall of our resolver vs SCIP ground truth, per language** |

That last row is the most valuable test in the suite: it quantifies exactly how approximate our
zero-install resolver is, per language — and produces a publishable table for the paper.

### 9.3 Property and metamorphic tests — oracle-free, every push
- `CC ≥ 1`; CC invariant under renaming, reformatting and comment insertion; monotone under adding a
  decision point.
- Cognitive: a sequence of k like operators scores exactly 1 for all k; alternating operators score
  the number of runs; wrapping a construct in one extra `if` increases the score by exactly
  (1 + the number of B3 structures inside).
- Halstead: `N = N₁+N₂`; consistent variable renaming leaves n₁, n₂, N₁, N₂ unchanged.
- `LCOM* ∈ [0,1]` for m > 1; `LCOM4 ≥ 1`; `LCOM4 ≤ LCOM3`, since call edges only merge components
  — which is why the constructor exclusion above is applied to both and never to one.
- Graph: every node in exactly one SCC; the condensation is a DAG; `propagation_cost ∈ [1/N, 1]`;
  `CCD ≥ N`.
- **Cross-language transliteration equivalence:** the same algorithm hand-written in all five
  languages must yield **equal CC and cognitive scores**, modulo a documented per-language exception
  list. This is the strongest possible test of language-agnosticism, and it is uniquely available to
  a polyglot engine.

### 9.4 Robustness and fuzz
Vendored small OSS repos per language at pinned SHAs. Assert: no crash, no unhandled node kind,
`ERROR`-node rate below threshold. **Tree-sitter `ERROR`/`MISSING` nodes must set
`parse_incomplete: true` and suppress affected metrics — never emit a silently wrong number.**

### 9.5 Performance regression
Assert p95 single-file `oxn check` < 200 ms including interpreter start, and a full 100 kLOC scan
< 30 s. **Also assert that the import graph of `oxn.cli` contains no heavy module** — a guard test
that fails if anyone adds a top-level `import networkx`/`numpy`/`torch`.

**CI shape:** `pytest -m "not oracle"` on every push (pure Python, no external tools);
`pytest -m oracle` nightly and on release, in the toolchain container. Oracles must never drift into
being required runtime deps.

---

## 10. Scoring, aggregation and threshold calibration

### 10.1 Two outputs, never conflated
1. **Gate** — boolean, blocking, scoped to *changed entities*, always explainable
   ("`process_order` cognitive complexity 14 > 8; the +3 at line 42 comes from an `if` nested 2
   deep"). **Never a score.** An agent cannot act on "your score is 71."
2. **Health rating** — informational, project- and component-level, trend-tracked.

### 10.2 Aggregation: risk profiles, not averages
Software metric distributions are heavy-tailed (Concas, Marchesi et al. 2007; Louridas, Spinellis &
Vlachos, "Power laws in software," *TOSEM* 18(1), 2008) — **means are meaningless**. Use the published
SIG method: Heitlager, Kuipers & Visser, *QUATIC 2007*; Alves, Correia & Visser, "Benchmark-based
aggregation of metrics to ratings," *IWSM/MENSURA 2011*.

Recipe: bucket every entity into **risk categories** (low / moderate / high / very-high) by threshold
→ compute the **percentage of LOC** in each category (a *risk profile*) → map the profile to a 1–5
rating via calibrated boundaries. Report the profile alongside the rating so the number decomposes.

### 10.3 Threshold calibration — three modes
- **Mode A (default): fixed literature thresholds.** CC ≤ 10 (McCabe 1976's own suggestion; NIST
  SP 500-235 discusses 10/15); cognitive ≤ 15 (**SonarQube's own default for methods**); params ≤ 5;
  max nesting ≤ 4; function ≤ 60 SLOC; file ≤ 500 SLOC.
  **Open question:** `idea.md` proposes `max_cognitive_complexity: 8`, nearly twice as strict as
  SonarSource's own default of 15. That should be a deliberate documented choice, not an accident —
  and it interacts badly with §10.5.
- **Mode B: self-calibration / ratchet — recommended for agent governance.** Derive the "high"
  boundary from the project's own p90 and store a baseline in `.oxn/baseline.json`. The gate becomes
  **"don't make it worse"**: block if a change raises the count of entities above the ratchet, or
  worsens the worst offender in a touched file. This is the right default for governing an agent on a
  legacy codebase, where Mode A fails on the first commit and gets disabled.
- **Mode C: benchmark-derived percentiles.** Alves, Ypma & Visser, "Deriving metric thresholds from
  benchmark data," *ICSM 2010*: weight each entity by LOC, aggregate the weighted distribution across
  a corpus of systems, take the 70th/80th/90th percentiles as risk boundaries. Requires a corpus →
  a research-track deliverable, and publishing OXN's per-language percentile tables would be a
  genuinely useful open artifact.

  **Measured, 2026-09-09** (`scripts/measure_ceilings.py`, five `use: threshold` corpora). The LOC
  weighting is load-bearing and not a detail: *un*weighted, these distributions have median 0 —
  69.4% of nest's callables are anonymous arrow functions and 92.7% of those score 0 — so an
  unweighted p95 for cognitive complexity is 2 in TypeScript against 9 in Go. Weighted as Alves
  et al. specify, the boundaries are sane and OXN's existing ceilings already sit on them:
  cognitive 12 at P79 (go-kit) to P98 (petclinic), cyclomatic 10 at P86–P99, nesting 4 at
  P96–P100. The ceilings were **not** refitted — the weighted p90 for cognitive complexity ranges
  7 to 23 across the five, so a fitted gate tracks whichever repositories were benchmarked. What
  the corpora contribute instead is each ceiling's *exceedance*, recorded in `oxn.calibration` and
  frozen in `benchmarks/ceiling-observations.json`. See ROADMAP P10.

#### Shipped 2026-09-09: profiles, and why the rating is not in them

`oxn health` implements 10.1's second output. Per declared ceiling it reports the share of source
lines in entities over budget and names the entities carrying it, weighted by SLOC rather than by
entity count for the reason 10.2 gives. It never blocks, and the layer contract keeps it out of
`check.py`.

**What is deliberately absent is the 1-5 rating and 10.4's composite.** The recipe above ends by
mapping a profile to a rating via calibrated boundaries; SIG derived those from roughly a hundred
systems and OXN has five — the same five its ceilings were measured against, so a rating fitted to
them would state how httpx compares to httpx. That is the circularity 10.3's amendment records for
the ceilings themselves, one step further along. The profile satisfies P10's exit criterion on its
own: a share that decomposes into named entities is explainable in the sense the criterion means,
and "3 stars" is not.

**The buckets are the declared ceilings, not per-language percentiles**, which would have meant
fifteen new tunables fitted to one repository each. A ceiling is already argued and already
recorded, and reusing it means "over budget" says the same thing in the gate and in the health
view rather than two things that drift.

### 10.4 Composite health score
An explicit weighted sum of per-dimension ratings — `{complexity, cohesion, coupling, architecture,
history}` — with weights in `oxn.yaml`, **always reported alongside its sub-ratings**. No magic
multiplicative formula. The single number exists for trend lines only. Prioritisation uses **hotspot
rank (churn × complexity)** to answer *what to fix first* — never *whether to block*.

### 10.5 Anti-gaming — a real research contribution
Agents optimise exactly what you measure. Blocking hard on cognitive complexity reliably produces a
function shredded into twenty one-line helpers with worse overall design — precisely the failure
"Clean Code, Better Models" (arXiv 2508.11958) hints at. Mitigations designed in from day one:
- Pair every per-function ceiling with a **class/module-level aggregate** (WMC, NOM, module SLOC,
  LCOM4), so shredding is also penalised.
- Track **helper proliferation**: functions called from exactly one site, defined in the same file,
  added in the same change.
- Gate on the **risk profile** (% of LOC in high-risk buckets), not just on max values — shredding
  moves LOC between buckets rather than eliminating it.
- Report the retry budget and *stop*. Convergence is not guaranteed; a bounded-retry design with
  honest convergence analysis is itself a paper contribution.

#### Measured 2026-08-30: complexity mass is *anti-correlated* with shredding

The first mitigation above was implemented in `scripts/dogfood.py` as **"total complexity mass
stays put while function count jumps"**. A hand-built control falsified it, and in the worst
possible direction. Three variants of the same function (`_imported_names`, 35, in
`src/oxn/resolve/scopes.py`), each spliced into the real file and measured:

| variant | target | file mass | functions | old rule said |
|---|---|---|---|---|
| original | 35 | 137 | 19 | — |
| cohesive extraction — 3 helpers scoring 4, 9, 11 | 2 | **128** | +3 | **shredded** ✗ |
| hand-built shred — 16 helpers, median 1 | 2 | **118** | +16 | **not shredded**, accepted ✗ |

The rule rejected the good refactoring and accepted the shred. The cause is a property of
cognitive complexity itself, not a mis-set threshold: **fundamental increments survive extraction
— an `if` is still an `if` wherever it lives — but nesting increments evaporate, because every
extracted helper restarts at depth zero.** Mass therefore falls *monotonically* as a function is
shredded harder, with a floor at the raw decision count. No denominator repairs this; a
subtree-scoped version of the same rule was tried and is backwards for the same reason.

**The Modular Mirage signature is substance dilution, not mass conservation.** What separates the
two cases is what the new helpers are individually worth:

| | new helpers | their scores | median |
|---|---|---|---|
| cohesive | 3 | 4, 9, 11 | **9** |
| shred | 16 | mostly 0–1 | **1** |

So the deterministic rule is now *many new helpers, each of them trivial* — at least
`MANY_HELPERS` (3) additions whose median score is at or below `TRIVIAL_HELPER` (2). Both figures
are calibrated on this single pair and are expected to move as `benchmarks/dogfood-log.jsonl`
accumulates real extractions; both live candidates are kept as regression fixtures in
`tests/test_dogfood.py`.

#### Measured 2026-08-31: the repair harness converges on a 35-point function and not on a 37-point one

Nine live attempts at `build_file` (cognitive complexity 37), across three fixes to the client
that stood between the model and a fair test:

| actor configuration | outcome |
|---|---|
| reasoning on, `num_predict` 8192 | ~31,900 characters of reasoning, cut off, **no answer** (×3) |
| reasoning on, `num_predict` 32768 | ~132,000 characters of reasoning, cut off, **no answer** (×3) |
| reasoning off | 131k–136k characters of reasoning *in the answer field*, **no usable answer** (×3) |

The middle row is the informative one: `glm-5.3:cloud` scales deliberation to whatever budget it
is given, so raising the limit buys a slower failure rather than a result. Turning reasoning off
does not stop it reasoning — it moves the reasoning into `response`, where the same 130k characters
arrive as prose punctuated by drafts.

Of the three answers-field replies, one contained **six** competing definitions of the target among
105 fenced blocks and two contained **none at all**. That first one exposed a harness bug worth
separating from the model result: extraction concatenated every fenced block, so six drafts and a
string literal severed mid-thought became one unparseable file, which the gauntlet then reported as
a *failed refactoring* rather than a failure to extract one. Fixed by selecting the last draft that
parses — a check rather than a guess, since on that reply the final draft was itself broken.

With extraction fixed, the honest result stands: the harness converged on `_imported_names` (35 → 2,
accepted, on the first attempt) and did not converge on `build_file` (37), because for this target
the actor never produced a complete function. The difference is not the score — it is that
`_imported_names` is a self-contained classifier and `build_file` is a tree walk with a closure over
five locals, which the model spent 130,000 characters failing to hold in mind at once.

That is a result about **this actor on this shape of function**, not a limit of the loop, and it is
what `benchmarks/dogfood-log.jsonl` exists to accumulate. It also sets the honest expectation for
P10: convergence is per-target, and the retry budget from arXiv 2508.11958 is not a formality.

#### Measured 2026-08-31: the gate itself had no such rule, and the evasion worked

Everything above describes the **dogfood harness**. The shipped gate had none of it: `oxn check`
enforced six per-entity maxima, and shredding reduces every one of them. Measured on a six-branch
router:

| | functions | max cognitive | `oxn check` |
|---|---|---|---|
| honest | 1 | **28** | 2 violations, exit 2 |
| shredded into dedicated helpers | 8 | **3** | **0 violations, exit 0** |

Worse than the gap itself, `oxn init` wrote into every user's `CLAUDE.md` that splitting into
one-line helpers "is detected and rejected as shredding" — a defence the gate did not have, told
to the one reader who would test it. Both are fixed: the rule below ships in `oxn.metrics.shredding`,
and the sentence is now true of the gate rather than of a script.

**The rule, and why it is scoped to a caller rather than a file.** The first version compared
trivial private helpers against the *file's* function count. It fired on 33 files across `src`,
`tests`, `scripts` and a vendored httpx — `store.py`, `render.py`, `cli.py` — and, far worse, the
same shred pasted into a 200-function module diluted below the threshold and passed. It worked on
the demo and failed on real code. What ships instead totals a function together with the helpers
that are *dedicated* to it — private, trivial, and called exactly once — and compares that total
against the same `cognitive_complexity` ceiling. On the same sweep: **zero false positives across
203 real violations**, and the shred still blocked at any file size.

The ceiling, not a new number, makes the decision. That ties the rule to what is being gamed and
keeps it from having an opinion about style: `render.py`'s five-section renderer totals 8 and
passes, because 8 was never a violation; the same shape over the router totals 13 and does not.

**Decisions are conserved under extraction; complexity mass is not.** Measured on the same pair,
total cyclomatic complexity minus one per function — the raw decision count — is *exactly* equal
before and after: 14 − 1 = **13** honest, 21 − 8 = **13** shredded. This is the floor predicted
above, confirmed. It is not used as a gate: a cohesive extraction conserves decisions too, so
gating on it would make the rule unsatisfiable by any refactoring. Reported, not enforced.

**What this rule cannot see.** The cluster total is a *reconstruction*, and a low one: nesting
increments evaporate under extraction, so 28 becomes a sum of 13. A shred of a marginal violation
therefore sums to under the ceiling and passes. The static rule catches the flagrant case; the
harness's differential rule, which has the before-state, catches the marginal one. Neither is a
completeness claim. Three further tripwires are calibration, not detection, and are recorded in
`oxn calibration` rather than chased: helpers written just above `TRIVIAL_HELPER`, helpers given a
public name, and helpers given a second call site.

A fourth is a limit of scope rather than of calibration, and is left open deliberately. Helpers
reached through a **dispatch table** are *referenced*, not called, so `callee_names` never sees a
call site and they are never candidates — verified: the same router shred, rewritten to dispatch
through a tuple of `(verb, prefix, handler)` rows, passes. Counting bare references instead of
calls was measured and rejected: it cannot tell a call from an export, a decorator or an
annotation, and the loose version mostly manufactures phantom second callers, which weakens
detection rather than strengthening it. The deeper reason to leave it is that turning branching
into data *is* a structural simplification — reading cost moves into a table you can read — and an
agent that evades this rule by writing a genuine dispatch table has done the work the rule was
asking for. Where a table is instead a disguise, only cohesion can say so, and cohesion is the
judge's job by design.

OXN's own `_walk` is the worked example in both directions: it went from cognitive complexity 52 to
7 by becoming a dispatch table over one handler per increment class, and the shredding rule
correctly does not fire on it.

Two consequences worth stating plainly:

* **Mass is reported and never gated.** It remains useful context and a trend signal; it is not
  evidence of gaming in either direction.
* **The judge becomes load-bearing.** Twelve helpers scoring 3 each would clear the deterministic
  rule, and only *cohesion* rejects that — which is the neural layer's declared job. Judge/gauntlet
  agreement in `dogfood.py report` is therefore the calibration signal for this whole design, not a
  decoration.

The third mitigation above (gate on the risk profile) is untouched by this and remains the right
instrument at module scope; the second (helper proliferation by call-site count) is **not** a
discriminator — the cohesive extraction's three helpers each have exactly one caller too.

#### Measured 2026-09-09: the risk profile fails the same control, harder

The sentence immediately above is wrong, and the same control falsifies it. The third mitigation
claims *"shredding moves LOC between buckets rather than eliminating it"*. Shredding eliminates it:
splitting a function is precisely the operation that moves every one of its lines into a lower
bucket, because the per-entity value falls when the entity gets smaller. The three variants, LOC
bucketed at Python's benchmark boundaries (`scripts/measure_ceilings.py`; cognitive 1/4/9,
cyclomatic 2/5/8), as **% of LOC in high + very-high**:

| variant | cognitive | cyclomatic | cyclomatic mass |
|---|---|---|---|
| original — one function, 35 | **100.0%** | **100.0%** | 17 |
| cohesive extraction — 3 helpers scoring 4, 9, 11 | 60.3% | 60.3% | 20 |
| hand-built shred — 16 helpers, median 1 | **0.0%** | **0.0%** | **33** |

A risk-profile gate would reject the good refactoring and award the shred a *perfect* score. That
is the 2026-08-30 inversion again, with a wider margin.

**Cyclomatic complexity does not rescue it, and the reason is worth recording.** Cognitive
complexity loses its nesting increments under extraction, which is why *mass* fell monotonically
in the table above. Cyclomatic has no nesting term, so its mass is conserved — and grows, one `+1`
base per new function, 20 → 33 here. That is why a cyclomatic profile looked like the fix. It is
not: mass rises while the distribution collapses, so under shredding the two signals point in
opposite directions at once. No per-function metric survives, because the failure is in the
aggregation, not the metric.

**What the risk profile is for, then.** Section 10.2's aggregation is still right for the *health
rating*, where being a distribution is the whole point, and it still decomposes so a number traces
back to named entities (§10.1). The gate shape it supports is the **ratchet** of §10.3 Mode B —
this module's high-risk share may not rise — not an absolute ceiling, and not an anti-gaming
instrument. Shredding detection stays where 2026-08-30 left it: the `shredding` rule, plus cohesion
and the judge. `tests/test_risk_profile.py` holds this result so it cannot be quietly re-adopted.

#### Measured 2026-09-09: the class aggregate survives, and it is the backstop for `shredding`

Two of this section's four mitigations have now failed a control. The **first** — pair every
per-function ceiling with a class-level aggregate — passes one, and the reason is worth having
in full, because it is the only mitigation here with a measured case behind it.

`shredding` clusters a function with the **private** helpers **only it calls**. Both premises are
evadable, and `TRIVIAL_HELPER`'s own `fit_when` in `oxn.calibration` already named the three
evasions. Six variants of one router class, the same routing work in each
(`tests/fixtures/class_scope/`, driven by `tests/test_class_scope_evasion.py`):

| variant | what it is | `oxn check` | NOM | WMC |
|---|---|---|---|---|
| before | one honest method, cognitive 28 | blocked (cognitive) | 1 | 14 |
| spread | split by HTTP verb — a *good* refactoring | **passes**, correctly | 4 | 17 |
| gamed | 14 private trivial helpers | blocked (`shredding` 13) | 14 | 27 |
| plump | each helper lifted above `TRIVIAL_HELPER` | blocked (`shredding` 19) | 14 | 39 |
| public | the same shred, public names | **passes** ✗ | 14 | 27 |
| shared | the same shred, every helper given a second caller | **passes** ✗ | 14 | 27 |

`plump` fails as an evasion — lifting the helpers above the threshold raises the cluster total
instead, 13 → 19. `public` and `shared` succeed: remove *private* or *only it calls* and there is
no cluster to total. **Both class aggregates separate them from the legitimate refactoring by a
wide margin** — NOM 4 against 14, WMC 17 against 27 — and any ceiling inside that margin catches
both escapes while passing `spread`. `before` sits below both and is caught by the cognitive
ceiling instead, which is the division of labour the pairing intends.

**Both ceilings earn their place, and that half of the evidence is not authored.** Positioned
by a single control, they would be exactly the circularity §10.3 warns about. So the ceilings
were also run as a census over the six corpora — code nobody wrote to be caught. They reject
**129 classes**: 72 by both, **31 by NOM alone** (many small methods, WMC under 25) and **26 by
WMC alone** (few heavy ones), so neither ceiling is a restatement of the other. And of the 98
rejected by WMC, **81 contain no method over the per-function cyclomatic ceiling** — every
method individually fine, the accumulation the whole problem. That is the God Class by
definition and it is invisible to every other gate OXN has; `GraphStore` is the local example
at WMC 64 with a worst method of 6. Frozen in `benchmarks/ceiling-observations.json` under
`class_ceiling_audit` and asserted in `tests/test_ceiling_observations.py`, so a future change
that makes either ceiling redundant shows up as a failing test rather than as symmetry nobody
rechecked.

**The known false positive, stated rather than hidden:** 8 of the 129 are test classes. Many
small test methods is a legitimate shape, and the answer is `oxn.yaml`'s advisory paths, not a
looser ceiling. 9 more are interfaces, where a large method count is an interface-segregation
smell rather than a God Class — a similar count, a different argument.

**The cost is where the decision is.** As shipped these reject 0.00–7.48% (NOM) and 0.27–10.28%
(WMC) of classes across the six corpora, against 0.00–10.57% for the per-function ceilings; a
class ceiling low enough to catch the escapes (NOM ≥ 8, WMC ≥ 18) fires on 7.4% of
OXN's own 176 classes and 6.5–19.2% across the corpora — because the escapes at NOM 14 sit
*inside* the legitimate distribution (p95–p99), not outside it. So this is a ratchet before it is
a ceiling: adopting it re-baselines, and the value is in blocking the *change* from NOM 1 to 14
rather than in condemning classes that were already large.

**Shipped as `methods_per_class: 12` and `weighted_methods_per_class: 25`**, both inside the
window the control defines (NOM 5..13, WMC 18..26) and deliberately not at its edges, since a
value tuned to straddle one fixture is fitted to that fixture. Measured across 3,501 class
entities they reject 0.00%–7.48% and 0.27%–10.28%, and 0.7%/2.0% of OXN's own 149 classes —
which became four baselined entries, `GraphStore` (NOM 27, WMC 64) among them.

Two containment facts decide what the population is, and both are language-specific:

* **The unnamed class entities are where the methods are** — or were, when these figures were
  taken. A Rust `impl` block is an unnamed class holding its type's methods, so ripgrep read 840
  class entities against 395 named ones, and filtering to named ones dropped its exceedance from
  3.10% to 0.25% — measuring the struct declarations rather than the code. The gate applies no
  name filter and neither does the calibration record, which is the opposite of the choice made
  for the callable ceilings, where anonymous density (0.1% of httpx, 69.4% of nest) makes `named`
  the only comparable row.

  Since then the `impl` join folds each block's methods onto the named type and leaves the block
  carrying no aggregate, so the unnamed *class* entities are no longer where Rust's methods are.
  The population stays unfiltered anyway: the gate does not filter, and Go's structs are unnamed
  entities either way. **The recorded exceedances predate the join and both receiver fixes and are
  due a re-measurement.**
* **Go needed receiver-based ownership, and now has it** *(2026-09-09)*. A Go method is a
  top-level declaration carrying its receiver rather than a member of its type — the same fact
  ADR-0002 records for `by_file` — so all 379 of go-kit's structs measured NOM 0 and both
  ceilings were **silently inert for the language**: a clean pass having checked nothing. Three
  things were wrong and all three were needed. A `type_declaration` carries no name (the
  `type_spec` it wraps does), so every Go type was an unnamed entity and a grouped
  `type ( Alpha …; Beta … )` collapsed into one. A `method_declaration` classified as
  `function`, because the method node kinds were TypeScript's and Java's and a Go type has no
  body to be "inside". And the join has to be by **package**, not by file: Go requires a method
  to be declared in the same package as its receiver type, and go-kit never exercises the split
  — all 376 of its methods sit in the same file as their type, none crossing the test/non-test
  boundary — but a file-scoped aggregate would report a different number the day a package was
  split across files, and a ceiling is compared against `.oxn/baseline.json`. An aggregate that
  means different things on different days cannot be compared against anything. go-kit now
  reads 494 method entities where it read none, a largest type of 9 methods, and `wmc` rejects
  0.27% of its types. `tests/test_go_class_ownership.py` holds the cross-file case.

**A curried callable was one entity, not two** *(2026-09-09)*. `_descend` handed a callable's
body to `_visit`, which iterates a node's *children* — so the body was never itself tested for
being a definition. That is the same thing for nearly every shape (an arrow whose body is a
call, a closure inside a block), and differs for exactly the curried one, where the body *is*
the next callable: `const add = (a) => (b) => a + b` and `add = lambda a: lambda b: a + b` each
produced a single entity. The inner callable was unmeasured, ungated, and missing from every
corpus count. Cost of the miss: 23 callables across nest's 15,020 and one in OXN's own source,
none in the other four corpora — small, but it made "nest has 14,997 callables" an artefact of
where the walk stopped rather than a measurement. `tests/test_curried_callables.py`.

**WMC was NOM under another name until this was measured.** `ck_metrics` computes
`sum(weights.get(name, 1) for name in model.methods)`, section 3.6 above specifies cyclomatic as
the default weight, `Evidence.complexity` existed to carry it and `run_classes` never filled it —
so every class in every corpus reported `wmc == nom`. Fixed with the measurement, because the
table above is meaningless without it.

---

## 11. Open decisions

1. **Gate policy for `APPROX` and `stale` metrics.** *Adopted as the default in ADR-0002:* only
   `EXACT` and fresh metrics may *block*; `APPROX` and `stale` metrics warn. Revisit only if it
   proves too permissive in practice.
2. **May `oxn init` install per-language toolchains and indexers, or only detect and instruct?**
   *Decided: detect and instruct only.* `oxn doctor` reports which SCIP indexers are present and
   prints the install command for those that are not; OXN never installs anything on a user's
   behalf. Encoded in `src/oxn/doctor.py` and ADR-0002.
3. **`.oxn/` cache location and `.gitignore` policy.** *Decided and implemented:* the cache lives
   at `.oxn/cache/graph.db` and is gitignored as a build artifact; `.oxn/baseline.json` is
   explicitly un-ignored, because the ratchet is shared state, not a derived one.
4. **Launch languages for the first metric milestone.** *Decided and shipped:* Python, TypeScript
   and JavaScript have full profiles; Go, Rust and Java have verified grammars and land as profiles
   in P6, alongside their gocognit and rust-code-analysis oracles.
5. **Cognitive complexity ceiling.** *Decided: 12.* `idea.md` proposed 8 and SonarSource's own
   default is 15; 8 flags a great deal of reasonable code, 15 lets agent-written functions through
   while still being hard to read. Recorded with its reasoning in `src/oxn/thresholds.py`. It is
   only safe paired with the anti-gaming aggregates of §10.5 — a per-function ceiling alone teaches
   an agent to shred one function into twenty one-line helpers.
6. **CI image may contain LGPL/GPL oracles** (eslint-plugin-sonarjs, code-maat). **Still open, and
   now load-bearing:** eslint-plugin-sonarjs (LGPL-3.0) is wired into the oracle lane as of P2. It
   is invoked as a subprocess, never linked or redistributed, which is licence-clean — but it is a
   decision worth making deliberately rather than by default.
7. **Halstead spec ownership.** *Decided and implemented:* OXN publishes its own table, versioned
   as `halstead_spec_version` on the profile. Confirmed necessary by measurement -- radon counts a
   strictly narrower operator set (§3.6), so chasing compatibility would have meant abandoning
   cross-language comparability.

---

## Sources

- [Cognitive Complexity white paper v1.7, G. Ann Campbell, SonarSource](https://www.sonarsource.com/docs/CognitiveComplexity.pdf)
- [Arcan documentation — architectural smells and thresholds](https://docs.arcan.tech/2.8.0/architectural_smells/) · [Arcan: A Tool for Architectural Smells Detection](https://boa.unimib.it/bitstream/10281/155470/1/PID4705339.pdf)
- [SCIP protobuf schema](https://github.com/sourcegraph/scip/blob/main/scip.proto) · [Stack graphs (Creager & van Antwerpen)](https://arxiv.org/pdf/2211.01224) · [Introducing stack graphs — GitHub Blog](https://github.blog/open-source/introducing-stack-graphs/)
- [lizard (MIT)](https://github.com/terryyin/lizard) · [complexipy (MIT)](https://github.com/rohaquinlop/complexipy) · [rust-code-analysis (MPL-2.0)](https://github.com/mozilla/rust-code-analysis)
- [import-linter (BSD-2)](https://github.com/seddonym/import-linter) · [tach (MIT)](https://github.com/tach-org/tach) · [dependency-cruiser (MIT)](https://github.com/sverweij/dependency-cruiser)
- [code-maat (GPL-3.0)](https://github.com/adamtornhill/code-maat) · [multilspy (MIT)](https://github.com/microsoft/multilspy) · [Joern (Apache-2.0)](https://github.com/joernio/joern)
