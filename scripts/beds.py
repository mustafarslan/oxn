"""Where the harness gets its targets: the evaluation beds.

P11 names three -- SlopCodeBench, the RealWorld Conduit constraint-decay setup, and real
layered OSS repositories -- and the harness knew none of them. It repaired OXN's own
functions and nothing else, with `src/oxn` written into `select_targets`. That is a fine bed
and a bad only-bed: a result measured solely on the repository the tool was written for is a
statement about that repository.

A bed is three things, and the third is why the external ones cannot simply be pointed at:

* **where the code is** -- a checkout under `benchmarks/corpora/`, fetched by
  `scripts/fetch_corpora.py` from the pins in `benchmarks/manifest.yaml`;
* **what to measure** -- the source paths, since a repository is not all source;
* **how to know a repair did not break it** -- the commands that must still pass. For this
  repository that is pytest, ruff and mypy. For SlopCodeBench it is that bench's own
  checkpoints, and for Conduit its API conformance suite.

**The declared beds carry no verification commands, and are refused rather than guessed at.**
Inventing a test command for a corpus nobody here has opened would produce a harness that
runs, reports, and measures nothing -- the failure this project keeps finding in its own
metrics. `benchmarks/manifest.yaml` already pins both with `use: eval`; fetching them is one
command, and filling in `verify` is a decision to make with the corpus in front of you.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPORA = ROOT / "benchmarks" / "corpora"


@dataclass(frozen=True, slots=True)
class Bed:
    """One place the harness may take targets from, and how to check what it did there."""

    name: str
    #: Relative to `benchmarks/corpora/`, or empty for this repository.
    corpus: str
    #: What `oxn metrics` measures inside the bed. A repository is not all source.
    sources: tuple[str, ...]
    #: Commands that must still pass after a repair. Empty means the bed cannot be run --
    #: see the module docstring; a bed that cannot verify cannot report.
    verify: tuple[tuple[str, ...], ...] = ()
    why: str = ""
    #: Populated for a declared bed that has no verification yet, and printed when refused.
    blocked_on: str = field(default="")

    @property
    def root(self) -> Path:
        return ROOT if not self.corpus else CORPORA / self.corpus

    @property
    def fetched(self) -> bool:
        return self.root.is_dir()

    @property
    def runnable(self) -> bool:
        return self.fetched and bool(self.verify)


#: This repository, which is the bed every result so far was measured on. Its verification is
#: the gauntlet's -- tests, lint, types -- and it is the only bed that needs no fetch.
SELF = Bed(
    name="self",
    corpus="",
    sources=("src/oxn",),
    verify=(("pytest", "-q"), ("ruff", "check"), ("mypy", "src")),
    why="OXN's own source. Honest about the tool, and a statement about one repository.",
)

BEDS: dict[str, Bed] = {
    "self": SELF,
    "slop-code-bench": Bed(
        name="slop-code-bench",
        corpus="slop-code-bench",
        sources=(".",),
        why=(
            "20 problems / 93 checkpoints, language-agnostic, measuring code erosion under "
            "iterative specification refinement -- the Tier 1.5 signals OXN computes "
            "natively, which is what makes it P11's primary bed. arXiv:2603.24755."
        ),
        blocked_on=(
            "its checkpoints are the verification and they are per problem, so `verify` is "
            "a decision to make with the corpus open rather than a command to guess"
        ),
    ),
    "realworld-conduit": Bed(
        name="realworld-conduit",
        corpus="realworld-conduit",
        sources=(".",),
        why=(
            "the Conduit API specification (19 endpoints) used by the constraint-decay "
            "study to layer architectural constraints L0..L3, which is the directly "
            "comparable setup for the constraint-budgeting arm. arXiv:2605.06445."
        ),
        blocked_on=(
            "Conduit is many implementations behind one API spec, so which one is under "
            "test -- and therefore how it is verified -- is a choice, not a lookup"
        ),
    ),
}


def bed(name: str) -> Bed:
    """The bed registered under `name`, refused with the reason when it cannot run.

    Three refusals, and they say different things on purpose: an unknown name is a typo, an
    unfetched bed needs one command, and a bed with no verification needs a decision. A
    harness that ran anyway would report a repair nothing checked.
    """
    found = BEDS.get(name)
    if found is None:
        raise SystemExit(f"unknown bed {name!r}. Available: {', '.join(BEDS)}")
    if not found.fetched:
        raise SystemExit(
            f"{name} is not fetched. Run:\n"
            f"    python scripts/fetch_corpora.py --use eval\n"
            f"which pins it from benchmarks/manifest.yaml into benchmarks/corpora/."
        )
    if not found.verify:
        raise SystemExit(
            f"{name} has no verification commands, so a repair there could not be checked.\n"
            f"    {found.blocked_on}.\n"
            f"Set `verify` on it in scripts/beds.py once that is decided."
        )
    return found


def bed_names() -> tuple[str, ...]:
    """Every declared bed, runnable or not: `--help` should show the experiment's shape."""
    return tuple(BEDS)
