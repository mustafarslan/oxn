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
        """Fetched, declaring verification, and not known to be unable to run it.

        `blocked_on` is part of this and was not: a bed whose `verify` is correct and whose
        corpus cannot be prepared answered True here while `bed()` refused it, so the two
        disagreed about the same bed. `runnable` is what a caller asks before offering a bed;
        it has to mean what the refusal means.
        """
        return self.fetched and bool(self.verify) and not self.blocked_on


#: The one httpx test that does not pass on its own untouched tree here. See the bed below.
_TRIO_TIMEOUT = "tests/test_timeouts.py::test_write_timeout[trio]"

#: go-kit's packages, minus `sd/eureka` and the whole of `metrics`. See the bed below.
#:
#: Go has no `--deselect`, so an exclusion has to be spelled as an inclusion. Written as the
#: eight groups that have no exclusion plus `sd` expanded, rather than as `./...`, because
#: the alternative is a bed that cannot verify its own pristine tree and therefore cannot
#: verify anything.
_GO_KIT_PACKAGES = (
    "./auth/...",
    "./circuitbreaker/...",
    "./endpoint/...",
    "./log/...",
    # **The whole of `metrics` is left out, for a different reason than `sd/eureka` and a
    # worse one: those tests are flaky, not broken.** `TestGauge` in `metrics/cloudwatch` was
    # the first one found -- three pristine runs giving FAIL, FAIL, ok -- and excluding that
    # single package was not enough: four runs of `./metrics/...` on the untouched tree
    # failed in `cloudwatch`, `prometheus` and `dogstatsd`, three runs out of four, with a
    # different subset each time.
    #
    # A deterministic failure blocks a bed and gets noticed. A coin-flip one makes every
    # `tests` verdict noise and lands in the log as repairs that sometimes break Go and
    # sometimes do not, which nothing reading it afterwards can tell from a real regression.
    #
    # `sources` below drops `metrics` as well, and that pairing is the point: excluding a
    # package from the tests while still selecting targets inside it would verify a repair
    # with a suite that no longer covers it, which is worse than not repairing it at all.
    "./ratelimit/...",
    "./tracing/...",
    "./transport/...",
    "./util/...",
    "./sd",
    "./sd/consul/...",
    "./sd/dnssrv/...",
    "./sd/etcd/...",
    "./sd/etcdv3/...",
    "./sd/internal/...",
    "./sd/lb/...",
    "./sd/zk/...",
)

#: The same list for `vet`, minus three more packages. **`vet` was left at `./...` when
#: `test` was narrowed, and it fails on the untouched tree too** -- eight diagnostics, every
#: one of them in a `_test.go` file and none of them anything a repair here introduces:
#: `t.Fatal` called from a non-test goroutine in `transport/nats` and `sd/consul`, and
#: `t.Errorf` copying a protobuf lock value in `transport/http/proto`. Newer vet checks
#: meeting older test code.
#:
#: `go vet -tests=false` does not help -- it still reports them -- so the exclusion is again
#: spelled as an inclusion, which is why `./transport/...` is expanded here and not above.
_GO_KIT_VET_PACKAGES = (
    "./auth/...",
    "./circuitbreaker/...",
    "./endpoint/...",
    "./log/...",
    "./metrics/...",
    "./ratelimit/...",
    "./tracing/...",
    "./transport",
    "./transport/amqp/...",
    "./transport/awslambda/...",
    "./transport/grpc/...",
    "./transport/http",
    "./transport/http/jsonrpc/...",
    "./transport/httprp/...",
    "./util/...",
    "./sd",
    "./sd/dnssrv/...",
    "./sd/etcd/...",
    "./sd/etcdv3/...",
    "./sd/eureka/...",
    "./sd/internal/...",
    "./sd/lb/...",
    "./sd/zk/...",
)

#: This repository, which is the bed every result so far was measured on. Its verification is
#: the gauntlet's -- tests, lint, types -- and it is the only bed that needs no fetch.
#:
#: **It has no targets when the gate passes, which is its normal state.** A target is a
#: callable over the ceiling, and `oxn check --deep` here reports 0 violations, so
#: `--bed self` answers "nothing over 12 in self" and stops. Every earlier `self` result was
#: therefore measured on a tree that happened to be dirty -- worth knowing before comparing
#: one to another. `--ceiling 8` is the honest way to get real OXN functions as targets: the
#: code is unchanged and the bar moves, rather than a violation being introduced to have
#: something to repair.
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
    # This project's own install, which used to be written into `Sandbox.create` and applied
    # to every bed. It belongs to the bed that has a `dev` extra.
    prepare=(("-m", "pip", "install", "-q", "-e", ".[dev]"),),
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
        sources=(
            "auth",
            "circuitbreaker",
            "endpoint",
            "log",
            "ratelimit",
            "sd",
            "tracing",
            "transport",
            "util",
        ),
        # `sd/eureka`'s *test binary* does not link on this toolchain: go-kit pins a
        # `golang.org/x/net` old enough that `internal/socket` still makes an unexported
        # reference to `syscall.recvmsg`, which Go 1.26's linker rejects. The package itself
        # builds and vets clean -- only linking a test binary against it fails -- so this is
        # the corpus's pin meeting a newer compiler, not anything a repair here did or could
        # fix. It failed identically before and after every attempt and took the whole bed
        # down with it, which is 53 targets lost to one package.
        #
        # Named out the way `_TRIO_TIMEOUT` is, and for the same reason: a check that fails
        # the same way whatever the repair says nothing about the repair, and refusing the
        # bed over it measures nothing at all. `vet` still covers `./...`, since vetting
        # never links.
        verify=(
            ("tests", "go", "test", *_GO_KIT_PACKAGES),
            ("lint", "go", "vet", *_GO_KIT_VET_PACKAGES),
        ),
        why="Go, explicitly modular: one package per concern, and the ceilings' Go corpus.",
    ),
    "rust-ripgrep": Bed(
        name="rust-ripgrep",
        corpus="rust-ripgrep",
        sources=(".",),
        # **`-D warnings` fails on the untouched tree**, and unlike go-kit's two named
        # packages the cause is not localised: clippy 1.98 against ripgrep's pinned code
        # reports at least a dozen distinct style lints -- `unnecessary_map_or` (7),
        # `new_without_default` (6), `collapsible_if` (6), `needless_borrow` (5),
        # `needless_lifetimes` (4) and more -- spread across crates. Every one is a
        # suggestion a later clippy added, not a defect a repair here introduced, and naming
        # them out would be a list that grows with clippy's release cadence rather than with
        # anything about this corpus.
        #
        # So the promotion to errors goes and `clippy` itself stays: it still fails the bed
        # on a genuine compile error, which is what "did the repair break this" asks. A check
        # that fails identically before and after a repair says nothing about the repair --
        # the same reason httpx deselects one test by name and go-kit names out two packages.
        verify=(("tests", "cargo", "test"), ("lint", "cargo", "clippy")),
        why="Rust, a workspace of crates with real boundaries between them.",
    ),
    "python-httpx": Bed(
        name="python-httpx",
        corpus="python-httpx",
        sources=("httpx",),
        # httpx keeps its dev dependencies in `requirements.txt`, which is what its own CI
        # installs, and has **no `dev` extra**. The sandbox used to install `-e .[dev]`
        # regardless -- and `uv` does not refuse a missing extra, it exits 0 having installed
        # nothing at all, so this bed had no `pytest` and every attempt scored `tests FAIL`.
        prepare=(("-m", "pip", "install", "-q", "-r", "requirements.txt"),),
        # ruff and mypy are pinned in that file, so all three checks are the ones httpx runs.
        verify=(
            # `test_write_timeout[trio]` fails on the *pristine* tree, deterministically,
            # three runs out of three: trio 0.31 garbage-collects an async generator that
            # httpx's pinned test expects to still be live, and pytest turns the
            # `ResourceWarning` into an error. The `[asyncio]` parametrisation passes. A
            # check that fails identically before and after a repair says nothing about the
            # repair and blocks every one of them, so it is deselected by name rather than
            # left to make the bed unusable -- and named here so that it is a decision.
            ("tests", "-m", "pytest", "-q", "-x", "--deselect", _TRIO_TIMEOUT),
            ("lint", "-m", "ruff", "check", "."),
            ("types", "-m", "mypy", "httpx"),
        ),
        venv=True,
        why="Python, and the corpus every Python ceiling was measured against.",
    ),
    "typescript-nest": Bed(
        name="typescript-nest",
        corpus="typescript-nest-bed",
        sources=("packages",),
        # **`npm test` cannot run without an install, and this bed declared no `prepare`** --
        # the same family of defect as httpx's, which installed nothing and scored `tests
        # FAIL` on every attempt. The command is `npm ci`, not `npm install`, because the
        # corpus ships a lock file and a bed that re-resolves versions per sandbox is not
        # verifying the pinned tree it was fetched to verify. It is not set, because at this
        # pin it cannot succeed -- see `blocked_on`.
        verify=(("tests", "npm", "test"), ("lint", "npm", "run", "lint")),
        blocked_on=(
            "nest's dependency tree does not install under `npm ci`, and this is upstream "
            "rather than a pin chosen badly. Three releases were tried on 2026-09-19 and "
            "each failed differently: v12.0.3 on graphql 17.0.2 against "
            "`@apollo/cache-control-types`' peer range of 14.x-16.x, v11.1.27 on "
            "`@typescript-eslint/eslint-plugin@8.60.0` wanting parser `^8.60.0` where the "
            "lock holds 8.59.3, and v11.1.20 on EUSAGE -- the workspace versions of "
            "`@nestjs/common` and `@nestjs/core` are simply absent from its lock. At every "
            "tag checked all 109-110 *direct* dependencies match the lock exactly, so the "
            "fault is in transitive peers and nothing short of running the resolver finds "
            "it. `--legacy-peer-deps` is not the answer: it installs a tree npm itself "
            "calls invalid, and a bed whose dependencies are wrong reports `tests FAIL` "
            "for reasons no repair caused -- the failure this file exists to stop "
            "repeating. **What changed is the price of the fix, not the verdict.** The bed "
            "now draws on `typescript-nest-bed`, a separate manifest entry from the "
            "`typescript-nest` threshold corpus, so adopting a release that does install "
            "is one line here and moves no recorded number. It used to mean re-pinning the "
            "checkout the ceiling, duplication and retrieval numbers were measured against"
        ),
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
            "two things, and the second is structural. The pinned repository is the "
            "specification and nothing else -- 156 Bruno and 13 Hurl request files under "
            "specs/api/, no implementation, since the hundred-plus implementations live in "
            "their own repositories -- so which one is under test has to be chosen and "
            "pinned. And its suite runs against a *live server*: "
            "`HOST=http://localhost:3000/api ./run-api-tests-hurl.sh`. Every `verify` here "
            "is a command run in a sandbox and judged by its exit code, which has no place "
            "to start a server, wait for it to be ready, give it a database, and stop it "
            "again. Conduit needs that lifecycle before it needs a `verify` line, and "
            "adding one to satisfy the field would produce a check that fails for the wrong "
            "reason on every repair"
        ),
    ),
}


def bed(name: str) -> Bed:
    """The bed registered under `name`, refused with the reason when it cannot run.

    Four refusals, and they say different things on purpose: an unknown name is a typo, an
    unfetched bed needs one command, a bed with no verification needs a decision, and a bed
    whose corpus cannot be prepared at its pin needs a re-pin. A harness that ran anyway
    would report a repair nothing checked.

    The fourth was added for typescript-nest, and the distinction it draws is the useful
    part: that bed's `verify` commands are right, and it still cannot run, because the pinned
    checkout's dependencies do not install. "No verification declared" and "verification
    declared but unrunnable here" are different states and used to be one.
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
    if found.blocked_on:
        raise SystemExit(
            f"{name} declares verification but cannot run it here:\n"
            f"    {found.blocked_on}.\n"
            f"Clear `blocked_on` in scripts/beds.py once that is no longer true."
        )
    return found


def bed_names() -> tuple[str, ...]:
    """Every declared bed, runnable or not: `--help` should show the experiment's shape."""
    return tuple(BEDS)
