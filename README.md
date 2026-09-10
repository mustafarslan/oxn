# oxn

A local architecture and quality gatekeeper for LLM coding agents.

**oxn** is short for *oxygen* — the thing you never notice until it runs out. Architectural
quality behaves the same way: invisible while it holds, and the only thing that matters once
it is gone. Agents burn through it faster than people do.

OXN computes software architecture and quality metrics itself — from one normalized code
graph, uniformly across languages — and uses them to hold coding agents to a project's
declared architectural invariants. Semantic context goes *in* over MCP; deterministic
enforcement comes *out* over hooks. One `pip install`, one `oxn init`, no cloud, no daemon,
no paid tooling.

> **Status: pre-alpha.** The scaffold and the design are in place; the analysis engine is
> being built. See **[ROADMAP.md](ROADMAP.md)** for the phase plan and
> **[docs/metrics.md](docs/metrics.md)** for the metric specifications.

## Why it computes its own metrics

Every tool that does *architecture* metrics well — Sonargraph, Structure101, NDepend,
Understand, Lattix, CodeScene — is commercial. The free complexity tools (radon, gocyclo,
complexipy, eslint-plugin-sonarjs) are each single-language or single-metric, so composing
them means several subprocess boundaries inside a per-edit hook, and metric definitions
that quietly disagree across languages.

So OXN takes exactly two free dependencies where the work is genuine commodity —
**tree-sitter** for parsing and **SCIP** for name resolution — and computes everything else
itself. The free tools become differential-test oracles in CI, which is what keeps
"self-implemented" credible rather than merely asserted.

The full rule and the classification of every tool considered: **[ADR-0001](docs/adr/0001-dependency-policy.md)**.

## Design decisions

| | |
|---|---|
| [ADR-0001](docs/adr/0001-dependency-policy.md) | Dependency policy — free-only, self-implement the rest |
| [ADR-0002](docs/adr/0002-resolution-strategy.md) | Name resolution — a tiered ladder with stamped outputs |
| [ADR-0003](docs/adr/0003-enforcement-model.md) | Enforcement — hooks enforce, MCP informs, constraints are budgeted |
| [ADR-0004](docs/adr/0004-freshness-model.md) | Freshness — how OXN answers quickly without a daemon |
| [ADR-0005](docs/adr/0005-rule-engine.md) | Rule engine — relations and conjunctive rules, Datalog-shaped |
| [ADR-0006](docs/adr/0006-retrieval-and-budgeting.md) | Retrieval — BM25 over decisions, budgeted, never blocking |

## Development

```sh
python -m pip install -e ".[dev]"
python scripts/check.py     # the fast lane: lint, types, unit tests
python scripts/check.py --all --install   # every lane, fetching what it needs
```

OXN runs either as the `oxn` console script or as a module, which is what you want when the
script is not on `PATH` -- an unactivated virtualenv, `uv run`, or a hook whose environment
you do not control:

```sh
oxn doctor                  # report grammars, toolchains and indexers found
python -m oxn doctor        # identical, no PATH required
python -m oxn metrics --explain src/
```

Lanes, in increasing cost:

| lane | what it runs |
|---|---|
| default | lint, types and the deterministic test suite -- no network, no toolchains |
| `--matrix` | the default lane on Python 3.10 through 3.14 |
| `--oracle` | differential tests against the third-party tools OXN deliberately does not depend on; needs Node, a JDK and Go |
| `--corpus` | phase exit criteria measured on real repositories |
| `--llm` | the few tests that need a language model, via Ollama |

GitHub Actions is manual-only by design -- `scripts/check.py` is this project's CI.

The `llm` lane uses Ollama with `glm-5.3:cloud`, overridable through `OXN_OLLAMA_MODEL` and
`OXN_OLLAMA_HOST`. It skips when the host is unreachable, and no other lane depends on a
model being up.

Benchmark corpora are pinned, never vendored:

```sh
python scripts/fetch_corpora.py --list
python scripts/fetch_corpora.py --use eval
```

## The hook, and what it says

`oxn init` writes a `PostToolUse` hook on `Edit`/`Write`/`MultiEdit`. On a violation it exits
2 and tells the agent what broke, at which line, and by how much:

```
OXN: this edit breaks 1 invariant(s). Fix them before moving on.

  src/api/client.py:6 api.client.send has cognitive_complexity 24, above the ceiling of 12  [attempt 1 of 3]
    +1 at line 8: `for`
    +2 at line 9: `if` nested 1 deep
    +3 at line 10: `for` nested 2 deep
```

That text goes to **stderr**, which is the stream Claude Code shows the agent on exit 2; the
JSON goes to stdout for CI and for `check_code`. Getting that backwards is not cosmetic, and
OXN had it backwards until 2026-09-05: the gate blocked every bad edit correctly and the
agent received the string `No stderr output`
([ADR-0003](docs/adr/0003-enforcement-model.md)'s amendment).

**The loop is bounded.** LLM refactoring does not reliably converge (arXiv 2508.11958), so
after `retry_budget` failed repairs of the *same* violation the hook stops asking and reports
instead, with the trajectory that shows whether the agent was getting anywhere:

```
OXN: retry budget spent. 3 repair(s) have not cleared 1 violation(s), and this is what each attempt scored:

  src/api/client.py:6 api.client.send — cognitive_complexity 24 -> 17 -> 17 -> 17 (ceiling 12)

Stop editing these entities and report to the user.
```

Attempts are counted per session and per violation, and repairing one forgets it. Set
`retry_budget: 0` in `oxn.yaml` to disable the bound — the right setting for CI, where there
is no agent to escalate to. Note the honest limit: a `PostToolUse` hook runs *after* the tool
and cannot end a turn, so the halt is an instruction rather than an enforcement.

The hook checks layer contracts too, on the edited file's own imports:

```
  src/oxn/graph/store.py:1 src/oxn/graph/store.py -> src/oxn/check.py: depends on a layer above it
```

It sees edges *out of* the files it measured. Edges *into* them from files nobody edited,
and import cycles, need the whole graph — that is `oxn check --deep`, and it is the CI
scope:

```sh
oxn check --deep          # exit 0 clean · 2 violations · 1 OXN itself failed to run
```

Exit 1 and exit 2 are deliberately different numbers: a gate that could not start must
never be read as a gate that passed. The retry budget does not apply in CI — it is keyed
on the agent `session_id` a hook sends and a pipeline does not — so CI always reports.

## The health view, which never gates

`oxn health` is the other output, and it decides nothing. Per declared ceiling it reports the
share of source lines sitting in code over budget and names the code carrying it — a risk
profile rather than a rating, because a 1–5 score needs calibrated boundaries and OXN has five
corpora, not SIG's hundred.

```sh
oxn health src/
```

```
  cognitive_complexity <= 12   4.1% of 12,043 lines over budget  (7 of 1,204 entities)
      21  oxn.graph.indexer.Indexer.index          src/oxn/graph/indexer.py:88

  coupling  no ceiling; distribution over 22 exactly-measured of 30 classes
      CBO  median 0   p90 0   max 3
      RFC  median 1   p90 3   max 7
      8 class(es) left a base or callee unplaced; their numbers are lower bounds
      >=  5 cbo  16 rfc  Indexer      src/oxn/graph/indexer.py
```

Coupling is the one dimension with no share and no ceiling. CBO and RFC have no entry in
`oxn calibration`, so no boundary here has a measured cost, and the numbers the literature
offers were fitted to systems this project has not measured. The `>=` is the other half of
that honesty: below L2 an unplaced base or callee makes a value a floor, so the distribution
covers the exactly-measured classes and the rest are counted and named rather than averaged in.

## The MCP server

`oxn init` writes `.mcp.json`, so an agent gets four read-only tools. They inform; the hook
is what enforces ([ADR-0003](docs/adr/0003-enforcement-model.md)).

| tool | what it answers |
|---|---|
| `get_architectural_context` | which constraints govern this task -- ranked, capped, and honest about what it left out |
| `check_code` | the gate's verdict on these files, without the power to stop anything |
| `get_metrics` | how the code measures, ranked by one metric |
| `explain_violation` | why one finding is a violation: the increment trail, and where its ceiling was declared |

```sh
oxn serve                    # speak MCP over stdio; stdout is the wire, so nothing is printed
claude mcp add oxn -- oxn serve
```

The protocol is hand-written against revision 2025-06-18 rather than taken from the MCP
Python SDK, which pulls 28 packages and two compiled wheels for an OAuth flow a stdio server
never performs — the measurement, and what would reverse it, are in
[ADR-0001](docs/adr/0001-dependency-policy.md)'s amendment. The SDK is a CI oracle instead:
`tests/test_oracle_mcp.py` drives `oxn serve` with the official client.

**`oxn` has to be on the PATH of the shell your editor starts the server with**, which an
unactivated virtualenv is not. `oxn init` detects that case and says so; the fix is a `pipx`
or `--user` install, or editing the `oxn` entry in `.mcp.json` to a path that resolves. Claude
Code expands variables there, so this repository's own entry reads

```json
{ "mcpServers": { "oxn": { "command": "${CLAUDE_PROJECT_DIR:-.}/.venv/bin/oxn", "args": ["serve"] } } }
```

which names the virtualenv without naming a machine, and mirrors the `PostToolUse` hook
beside it. A hand-edited entry is left alone on every later `init`.

The server also answers about the *repository*, not about wherever your editor happened to
start it: it moves to the project root on startup, and `--root` or `OXN_ROOT` overrides that
when discovery would find the wrong tree.

## `@oxn` on a pull request

`oxn review` measures what a pull request changed and, optionally, writes the comment.

```console
$ oxn review --base origin/main
origin/main...HEAD  12 changed, 10 measured, 2 excluded
  1 new  0 regressed  4 pre-existing
  new         parse has cognitive_complexity 16, above the ceiling of 12   src/a.py:44
  baselined   Store has weighted_methods_per_class 31, above the ceiling of 25   src/c.py:3
```

`oxn init --github` writes the workflow that runs it: tag `@oxn` in a pull-request comment and
the job measures the diff and posts a summary. It is opt-in, and an existing workflow file is
never overwritten.

**It does not gate.** Your CI `oxn check` job does that. Two gates disagreeing about one pull
request is worse than one gate.

### The numbers come from OXN and the sentences come from a model, and the second may not
invent the first

`--write ollama` hands the *measurement* to a language model and asks for prose. Every numeral
in the reply is then checked against the measurement, and a reply stating a number OXN did not
measure is **refused** — no comment, rather than a comment with a caveat, because a reader who
sees prose does not audit it. There is one retry, shown exactly which numbers were rejected,
and no second one: a tool whose honesty depends on how many times it asked is not honest.

The model is never shown the diff. A model given code reviews the code; this one restates
measurements. It also means a workflow triggered from a fork never hands that fork's contents
to a model.

Two limits worth knowing. A finding's `origin: new` means *not in `.oxn/baseline.json`* — not
*caused by this pull request*; a new violation can sit in an untouched function of a changed
file, and telling those apart needs a second checkout. And only the Ollama backend is
implemented: Anthropic, OpenAI and Gemini slot into the same `Writer` protocol in
`oxn/writers.py`, and shipping clients that have never made a request would claim four
providers where there is evidence for one.

## Self-repair

OXN gates coding agents on complexity, and used to violate its own ceilings.
`scripts/dogfood.py` drives the loop OXN exists to create, on OXN itself — a loop that has
now run out of work here: **`src`, `scripts` and `tests` report zero violations and the
baseline is empty**, so `plan` returns nothing unless you lower `--ceiling`.

```sh
python scripts/dogfood.py plan               # what is over the ceiling, and why
python scripts/dogfood.py repair --limit 3   # attempt repairs, write diffs for review
python scripts/dogfood.py repair --dry-run   # exercise the loop with no model calls
python scripts/dogfood.py report             # convergence, and the arm table
```

The same loop is the evaluation harness, along three axes it varies independently:

```sh
--bed   self | go-kit | rust-ripgrep | python-httpx | typescript-nest | java-spring-petclinic
--arm   none | claude-md | mcp | hooks | hybrid | hybrid-budget
--backend  ollama | claude-code | dry-run
```

A **bed** is where targets come from and *how a repair is checked there* — `go test` and
`go vet` for Go, `cargo clippy` for Rust, this project's pytest/ruff/mypy for itself. A check
a bed does not declare is skipped rather than failed, since a Go module has no type checker;
a declared check whose tool is missing is a failure, because then the repair was not verified.

An **arm** is which of OXN's three channels the agent gets: the rules `oxn init` writes into
`CLAUDE.md`, the increment trail `explain_violation` returns over MCP, and the hook rejecting
an edit for the agent to answer. `none` is the control and gets one attempt — without a
rejection there is nothing to retry against, and extra rounds would be re-rolls of the dice
scored as though feedback had helped.

`scripts/experiment.py` runs a whole grid, which is the only way to get a table worth reading:

```sh
python scripts/experiment.py --dry-run --arms none,hybrid --repeat 3   # price it first
python scripts/experiment.py --bed self --arms none,hybrid --repeat 3
```

It resolves the targets **once** and hands the same list to every arm. Running the harness by
hand per arm does not, and the first pilot's table read "none 100%, hybrid 0%" for exactly
that reason — two invocations, two different functions, one plausible-looking finding. The
`n` column and the note under the table exist for the same kind of reason: `generate` is
temperature 0 and not deterministic, so a rate over one sample per target cannot be told
apart from noise and should not be read as though it can.

Two models, deliberately different: **glm-5.3** writes the repair, **deepseek-v4-pro**
assesses it. A model grading its own output is not an independent check. `--backend
claude-code` drives `claude -p` instead, which is the arm that decides what a result means:
Claude Code under OXN's own hooks is the agent the tool exists to govern.

**The judge never overrules the deterministic gauntlet.** A candidate that fails tests,
types, lint, or the shredding detector is rejected before a judge sees it — because "the
score went down" is precisely the gaming a per-function ceiling invites, and detecting it
needs a measurement, not an opinion. Complexity *mass* was the obvious measurement and it
turned out to be exactly wrong — it ranks a shred above a good refactoring, for a reason
that is a property of the metric rather than a bad threshold (`docs/metrics.md` §10.5).
What works is what the helpers are worth: a cohesive split yields helpers with bodies, a
shred yields lines with names.

Nothing touches the working tree. Every attempt runs in a throwaway copy with its own
virtualenv, and the loop emits diffs plus a JSONL log for a human to review and commit.
That log is also the convergence evidence: LLM refactoring is known to fail to reach
complexity thresholds ([arXiv 2508.11958](https://arxiv.org/abs/2508.11958)), so measuring
it on a real codebase is worth having.

## Licence

MIT. See [LICENSE](LICENSE).
