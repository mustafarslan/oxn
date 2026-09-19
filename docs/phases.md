# The phase record

OXN was built in numbered increments, `P0` through `P12`, and those labels are all over this
repository: in 44 commit subjects, in the Status line of most ADRs, in docstrings, and in this
project's own benchmark data. The planning documents that *defined* them are not in this
repository and are not coming back.

This page is what the labels meant. It is a **retrospective, not a plan** — there is a `status`
column and deliberately no `next` column, and nothing here is a commitment. Where a phase's
scope cannot be reconstructed from the commits, the row says so rather than inventing it.

Each increment closed against criteria that were measured rather than asserted. Those
measurements live in [`metrics.md`](metrics.md), in the [ADRs](adr/), and in the `--corpus`
test lane; this page does not restate them.

## The increments

| Phase | What it delivered | Status | First commit |
|---|---|---|---|
| **P0** | Package scaffold, CI, grammar verification | shipped | `449f04f` |
| **P1** | Parse layer and the code-graph IR | shipped | `c5c2d18` |
| **P2** | Tier-1 metrics, and the differential-oracle harness that keeps "self-implemented" honest | shipped | `89b52fc` |
| **P2.5** | `oxn check`, `oxn init`, the baseline ratchet, the calibration surface | shipped | `f065da4`, `76d938c` |
| **P3** | Volume/erosion and VCS behavioural tiers | shipped | `37d7e88` |
| **P4** | Module graph, layer contracts, Tier-2 architecture metrics | shipped | `b007938` |
| **P5** | Name resolution: SCIP ingestion as the L2 rung, then L0/L1 graded against it | shipped | `0cd5b7b`, `a7790c2` |
| **P6** | Tier-3 cohesion, coupling and call-graph metrics; Go, Rust and Java profiles | shipped | `963e6c8`, `aea79db` |
| **P7** | The rule engine — an ADR or `oxn.yaml` can state an invariant, and it gates | shipped | `041397a` … `7fefd3b` |
| **P8** | Retrieval and the constraint bundle: BM25 over decisions, typed, ranked, capped | shipped | `fb5b63f` … `b10ffaf` |
| **P9** | The MCP server, the hook's contract, the CI gate, monorepo resolution | shipped | `447d299` … `0e6a9ab` |
| **P10** | Ceilings measured against corpora, class aggregates gated, `oxn health` | **closed** `fdddb8c` | `83ad9e2` |
| **P11** | The evaluation harness: six arms, six runnable beds, the measures, the grid driver | **harness shipped; the grid has not been run** | `e0825df` … `3cdd56e` |
| **P12** | Discovered modules against declared ones; the minimum edit that breaks a cycle | three items declined, one deferred with its blocker named (`53d15c9`) | `c214eda`, `85f7e0c` |

## Two rows that need more than a word

**P10 is closed, and its open list was audited twice.** `fdddb8c` closed it; `61b274d` then
found that the list of remaining items still named two things that had shipped and one nobody
intended to build. Both corrections are in the history rather than in a tidied summary, which is
the pattern this project prefers.

**P11's harness exists; the experiment has not run.** `scripts/arms.py`, `beds.py`, `actors.py`
and `experiment.py` are real, tested code, and `oxn review` came out of the same phase. What has
not happened is the twelve-cell grid being driven to a published table. The blocker is inference
budget, not design: commit `7453420` records 39 of 44 attempts in one run returning
`HTTP 429 -- rate limited, retry later`, and a deliberate stop once the endpoint began refusing.
Nothing in this repository reports an effect of OXN on an agent, and `oxn calibration` says the
same thing about the parameters that experiment would fit.

## Why the labels were kept

They could not honestly be removed. They are in 44 public commit subjects, and 17 of those
subjects are *pinned query text* in `benchmarks/retrieval-labels-oxn.json` — rewording them
would move a recorded measurement. Stripping the labels from the working tree would have left
them in the history and in the benchmark data with nowhere to look them up, which is worse than
leaving them and writing this page.

Where a label was printed to users by the installed tool, it was replaced with what it named —
the claim survives, the pointer does not. Everywhere else the label stands, and this table is
what it refers to.
