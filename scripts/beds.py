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
metrics.

Both are now fetched and opened, and both need more than a `verify` line -- which is exactly
why guessing one would have been wrong. SlopCodeBench is an agent-evaluation *harness*, not a
corpus: it drives the agent, so using it inverts this loop rather than feeding it, and its
problems are in a separate repository this pin does not include. RealWorld is the Conduit
*specification* and contains no implementation at all; its Hurl and Bruno collections are a
real conformance suite, and what must be chosen first is which of the hundred-plus
implementations is under test. `blocked_on` on each says what was found rather than what was
assumed, which is the difference between a note and a next step.
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
    #:
    #: `scripts/gauntlet.py` does **not** read this yet: it builds a `uv` venv, installs
    #: `-e .[dev]`, and runs this project's own pytest, ruff and mypy. Those happen to be
    #: `self`'s commands, so the two agree today by coincidence rather than by wiring.
    #: Filling this in for a second bed is therefore half the job -- the gauntlet has to
    #: learn the bed's toolchain too -- and that is the honest content of `blocked_on`.
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
        sources=(),
        why=(
            "20 problems / 93 checkpoints, language-agnostic, measuring code erosion under "
            "iterative specification refinement -- the Tier 1.5 signals OXN computes "
            "natively, which is what makes it P11's primary bed. arXiv:2603.24755."
        ),
        blocked_on=(
            "it is an agent-evaluation harness, not a corpus to repair -- SCBench drives "
            "the agent itself. Integrating it inverts this harness: OXN's arms become a "
            "configuration of *its* agent rather than a bed this loop repairs. It also "
            "needs Docker, an API key, and the problems, which live in a separate "
            "repository (gabeorlanski/scb-problems) that this pin does not include. Its "
            "own src/ and tests/ could be repaired like any Python project, and that would "
            "measure the benchmark's source instead of the benchmark"
        ),
    ),
    "realworld-conduit": Bed(
        name="realworld-conduit",
        corpus="realworld-conduit",
        sources=(),
        why=(
            "the Conduit API specification (19 endpoints) used by the constraint-decay "
            "study to layer architectural constraints L0..L3, which is the directly "
            "comparable setup for the constraint-budgeting arm. arXiv:2605.06445."
        ),
        blocked_on=(
            "the pinned repository is the specification and nothing else -- 156 Bruno and "
            "13 Hurl request files under specs/api/, and no implementation at all, since "
            "the hundred-plus implementations live in their own repositories. The good "
            "news is that those request collections *are* the verification, so `verify` "
            "here is knowable rather than inventable: it is `hurl` against a running "
            "implementation. What has to be chosen and pinned first is which "
            "implementation, and in which language, since that decides what OXN is even "
            "governing"
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
