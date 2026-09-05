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
or `--user` install, or editing the `oxn` entry in `.mcp.json` to a path that resolves — a
hand-edited entry is left alone on every later `init`.

## Self-repair

OXN gates coding agents on complexity, and used to violate its own ceilings.
`scripts/dogfood.py` drives the loop OXN exists to create, on OXN itself — a loop that has
now run out of work here: **`src`, `scripts` and `tests` report zero violations and the
baseline is empty**, so `plan` returns nothing unless you lower `--ceiling`.

```sh
python scripts/dogfood.py plan               # what is over the ceiling, and why
python scripts/dogfood.py repair --limit 3   # attempt repairs, write diffs for review
python scripts/dogfood.py repair --dry-run   # exercise the loop with no model calls
python scripts/dogfood.py report             # convergence across all attempts so far
```

Two models, deliberately different: **glm-5.3** writes the repair, **deepseek-v4-pro**
assesses it. A model grading its own output is not an independent check.

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
