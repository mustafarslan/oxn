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

## Development

```sh
python -m pip install -e ".[dev]"
pytest -m "not oracle"      # default lane: pure Python, no external tools
oxn doctor                  # report grammars, toolchains and indexers found
```

The `oracle` lane runs differential tests against the third-party tools OXN deliberately
does not depend on. It needs Node, Go, a JDK and Cargo, so it runs nightly rather than on
every push.

Benchmark corpora are pinned, never vendored:

```sh
python scripts/fetch_corpora.py --list
python scripts/fetch_corpora.py --use eval
```

## Licence

MIT. See [LICENSE](LICENSE).
