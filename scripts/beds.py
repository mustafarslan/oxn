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
    #: What to run before verifying: install dependencies, build, whatever the bed needs.
    #: Empty is fine -- a Go module needs nothing, and this repository's own step is the one
    #: exception complicated enough to stay in `Sandbox`.
    prepare: tuple[tuple[str, ...], ...] = ()
    #: Commands that must still pass after a repair, each `(label, *argv)`. Empty means the
    #: bed cannot be run: a bed that cannot verify cannot report.
    #:
    #: The label is what the failure is filed under -- `tests`, `lint`, `types` -- because
    #: the gauntlet reports those three separately and the actor is told which one it broke.
    #: A bed with no type checker simply has no `types` entry, rather than a fake pass.
    verify: tuple[tuple[str, ...], ...] = ()
    #: True for the one bed whose sandbox needs a `uv` venv and an editable install. Kept as
    #: a flag rather than a `prepare` entry because it also decides *how* commands run --
    #: through the sandbox's own interpreter rather than the ambient one.
    venv: bool = False
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
    verify=(
        ("tests", "-m", "pytest", "-m", "not oracle and not llm", "-q", "-x"),
        ("lint", "-m", "ruff", "check", "."),
        ("lint", "-m", "ruff", "format", "--check", "."),
        ("types", "-m", "mypy"),
    ),
    venv=True,
    why="OXN's own source. Honest about the tool, and a statement about one repository.",
)

#: P11's third bed category -- "real layered OSS repos" -- and they are already pinned as
#: `use: threshold` and already on disk, because the ceilings were calibrated against them.
#: One per supported language, each chosen for a layered or explicitly modular architecture,
#: which is what makes a layer-conformance result mean anything.
#:
#: **Their `verify` is their own toolchain, not this project's.** A Go module runs `go test`,
#: a Rust crate `cargo test`, and neither has any use for ruff. Where a toolchain may be
#: absent from the machine the gauntlet reports that as a skipped check rather than a pass --
#: a repair verified by commands that never ran is the failure this whole mechanism exists
#: to prevent.
_OSS: dict[str, Bed] = {
    "go-kit": Bed(
        name="go-kit",
        corpus="go-kit",
        sources=(".",),
        verify=(("tests", "go", "test", "./..."), ("lint", "go", "vet", "./...")),
        why="Go, explicitly modular: one package per concern, and the ceilings' Go corpus.",
    ),
    "rust-ripgrep": Bed(
        name="rust-ripgrep",
        corpus="rust-ripgrep",
        sources=(".",),
        verify=(("tests", "cargo", "test"), ("lint", "cargo", "clippy", "--", "-D", "warnings")),
        why="Rust, a workspace of crates with real boundaries between them.",
    ),
    "python-httpx": Bed(
        name="python-httpx",
        corpus="python-httpx",
        sources=("httpx",),
        verify=(("tests", "-m", "pytest", "-q", "-x"),),
        venv=True,
        why="Python, and the corpus every Python ceiling was measured against.",
    ),
    "typescript-nest": Bed(
        name="typescript-nest",
        corpus="typescript-nest",
        sources=("packages",),
        verify=(("tests", "npm", "test"), ("lint", "npm", "run", "lint")),
        why="TypeScript, and a framework whose whole subject is layering.",
    ),
    "java-spring-petclinic": Bed(
        name="java-spring-petclinic",
        corpus="java-spring-petclinic",
        sources=("src/main/java",),
        verify=(("tests", "./mvnw", "-q", "test"),),
        why="Java, layered controller/service/repository -- the contract OXN checks by name.",
    ),
}

BEDS: dict[str, Bed] = {
    "self": SELF,
    **_OSS,
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
