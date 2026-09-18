"""Where the harness takes targets from, and what it refuses to do without.

The harness repaired OXN's own functions and nothing else -- `src/oxn` was written into
`select_targets`. That is a fine bed and a bad only-bed: a result measured solely on the
repository the tool was written for is a statement about that repository, which is exactly
why P11 names three.

The interesting behaviour here is the refusing. `benchmarks/manifest.yaml` has pinned
SlopCodeBench and Conduit as `use: eval` all along, so pointing at them is easy and running
them is not: a bed needs verification commands, and inventing a test command for a corpus
nobody has opened produces a harness that runs, reports, and checks nothing. That is the
failure this project keeps finding in its own metrics, so it is a hard error with the reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.test_dogfood import load_harness

#: The corpus beds need a checkout, and the repair harness's own sandbox deliberately omits
#: `benchmarks/corpora` -- it is hundreds of megabytes and no repair needs it. So these must
#: *skip* where the corpora are absent rather than fail: a sandbox running the suite would
#: otherwise report `tests FAIL` for every candidate, whatever the model wrote, and no repair
#: could ever be accepted. Found by running the harness, which is the only way it shows.
CORPORA = Path(__file__).resolve().parent.parent / "benchmarks" / "corpora"
needs_corpora = pytest.mark.skipif(
    not (CORPORA / "go-kit").is_dir(),
    reason="corpora not fetched; see scripts/fetch_corpora.py",
)


@pytest.fixture(scope="module")
def harness():
    return load_harness()


def test_the_self_bed_is_runnable_and_is_this_repository(harness) -> None:
    """The bed every result so far was measured on, and the only one needing no fetch."""
    found = harness.SELF
    assert found.fetched and found.runnable
    assert found.sources == ("src/oxn",)
    assert (found.root / "src" / "oxn").is_dir()
    assert found.verify, "a bed that cannot verify cannot report"


@needs_corpora
def test_a_fetched_bed_stops_asking_to_be_fetched(harness) -> None:
    """Both eval beds are pinned in the lock and on disk, so the refusal moves on.

    It moves to the *right* refusal: fetching them was the easy half, and opening them is
    what showed the hard half -- neither is a tree of code this loop can repair.
    """
    for name in ("slop-code-bench", "realworld-conduit"):
        assert harness.BEDS[name].fetched, f"{name} is not fetched; run fetch_corpora --use eval"
        with pytest.raises(SystemExit) as raised:
            harness.bed(name)
        assert "no verification commands" in str(raised.value)


def test_an_unfetched_bed_would_say_the_one_command_that_fixes_it(harness) -> None:
    """The other refusal, exercised on a bed that cannot be on disk."""
    absent = harness.Bed(name="not-here", corpus="not-here", sources=())
    assert not absent.fetched
    harness.BEDS["not-here"] = absent
    try:
        with pytest.raises(SystemExit) as raised:
            harness.bed("not-here")
        assert "not fetched" in str(raised.value)
        assert "fetch_corpora.py --use eval" in str(raised.value)
    finally:
        del harness.BEDS["not-here"]


def test_a_bed_with_no_verification_is_refused_rather_than_guessed_at(harness, tmp_path) -> None:
    """The refusal that matters: a repair nothing checks is worse than no repair.

    Its message carries *why* rather than only *what*, because the fix is a decision made
    with the corpus open -- SlopCodeBench verifies by its own per-problem checkpoints -- and
    a reader who is only told "no verify" would reach for a plausible default.
    """
    declared = harness.BEDS["slop-code-bench"]
    assert not declared.verify
    assert "agent-evaluation harness" in declared.blocked_on, "the reason must be the real one"

    conduit = harness.BEDS["realworld-conduit"]
    assert not conduit.verify
    assert "no implementation" in conduit.blocked_on
    assert "live server" in conduit.blocked_on, "the structural half must be recorded too"

    fetched = harness.Bed(
        name="fetched-but-unverifiable", corpus="", sources=("src",), blocked_on="nothing decided"
    )
    assert fetched.fetched, "the repository root always exists"
    assert not fetched.runnable, "fetched is not enough; it must be checkable"


def test_an_unknown_bed_is_refused_with_the_list(harness) -> None:
    with pytest.raises(SystemExit) as raised:
        harness.bed("slop-code-bnch")
    assert "slop-code-bnch" in str(raised.value) and "slop-code-bench" in str(raised.value)


def test_every_declared_bed_says_why_it_is_a_bed(harness) -> None:
    """A bed with no rationale is a corpus someone happened to have.

    P11's beds were each chosen for a reason -- SlopCodeBench measures the erosion signals
    OXN computes natively, Conduit is the constraint-decay study's own setup -- and the
    reason is what makes a result comparable to the literature it cites.
    """
    for name, found in harness.BEDS.items():
        assert found.why, f"{name} has no rationale"


def test_every_bed_is_pinned_and_every_eval_pin_is_a_bed(harness) -> None:
    """Two directions, and each catches a different lie.

    A bed the manifest does not pin can never be fetched. And a `use: eval` pin that no bed
    names is a download nothing consumes -- which is exactly what SlopCodeBench and Conduit
    were until they became beds.

    The five OSS beds are pinned `use: threshold`, because they are the same repositories
    the ceilings were calibrated against. That is a feature: P11 asks for "real layered OSS
    repos" as a bed, and these are already chosen, already argued for, and already on disk.
    """
    import re
    from pathlib import Path

    manifest = (Path(harness.ROOT) / "benchmarks" / "manifest.yaml").read_text()
    blocks = dict(re.findall(r"- name: (\S+)\n(.*?)(?=\n  - name:|\Z)", manifest, re.S))
    declared = {name for name, found in harness.BEDS.items() if found.corpus}

    assert declared <= set(blocks), f"unpinned beds: {sorted(declared - set(blocks))}"
    eval_pins = {name for name, block in blocks.items() if "use: eval" in block}
    assert eval_pins <= declared, f"eval pins nothing consumes: {sorted(eval_pins - declared)}"


@needs_corpora
def test_there_is_a_runnable_bed_for_every_supported_language(harness) -> None:
    """Six languages in the goal, six runnable beds, one per language plus this repository.

    A result measured only on Python would say nothing about the five languages whose
    metrics were fixed today, and the beds are what stop that.
    """
    runnable = {name for name, found in harness.BEDS.items() if found.runnable}
    assert {"self", "go-kit", "rust-ripgrep", "python-httpx"} <= runnable
    assert "java-spring-petclinic" in runnable

    # **TypeScript has no runnable bed, and that is recorded rather than asserted away.**
    # The pin's `package-lock.json` is out of sync with its `package.json`, so `npm ci`
    # refuses and `npm install` abandons the pin only to hit a real peer conflict. The bed's
    # `verify` commands are right; nothing can install what they run against. Until the
    # corpus is re-pinned, five of the six languages have a bed and the sixth does not.
    assert not harness.BEDS["typescript-nest"].runnable
    assert harness.BEDS["typescript-nest"].blocked_on, "and it must say why"


def test_a_bed_declares_its_own_toolchain_and_not_this_projects(harness) -> None:
    """`go vet` is not `ruff`, and a Go module has no type checker to run.

    The gauntlet ran this project's three commands against whatever it was given. That was
    invisible with one bed and would have graded a Rust repair with Python's linter.
    """
    go = harness.BEDS["go-kit"]
    # The packages are named out one by one -- `sd/eureka` will not link and the whole of
    # `metrics` is flaky -- so what matters here is that the command is `go test`, not which
    # packages follow it. Those are pinned in `test_dogfood.py` where the reasons live.
    assert any(command[:2] == ["go", "test"] for _label, *command in go.verify)
    assert not any("ruff" in command for _label, *command in go.verify)
    assert not go.venv, "a Go module needs no virtualenv"
    assert "types" not in {label for label, *_ in go.verify}, "and no type checker"


def test_the_sandbox_copies_the_bed_and_not_always_this_repository(harness, tmp_path) -> None:
    """The defect a second bed would have hit silently: measure one tree, verify another.

    `Sandbox` copied `ROOT` unconditionally while `select_targets` also hardcoded `src/oxn`,
    so the two agreed and nothing showed. Point the harness at a different bed and the
    repair would have been graded against OXN's tests. It is unreachable today because every
    other bed refuses -- and "it can only fail to fire" is not a property to rely on in
    something whose output is a measurement.
    """
    elsewhere = tmp_path / "other-repo"
    elsewhere.mkdir()
    box = harness.Sandbox("probe", root=elsewhere)
    assert box.root == elsewhere
    assert harness.Sandbox("probe").root == harness.ROOT, "the default is still this repository"


@needs_corpora
@pytest.mark.parametrize(
    ("bed_name", "suffix"),
    (
        ("go-kit", ".go"),
        ("rust-ripgrep", ".rs"),
        ("typescript-nest", ".ts"),
        ("java-spring-petclinic", ".java"),
        ("python-httpx", ".py"),
    ),
)
def test_a_bed_finds_its_own_files_and_not_this_repositorys(harness, bed_name, suffix) -> None:
    """Targets and their explanation must both come from the bed. Neither did.

    `select_targets` found go-kit's files correctly and `_trail_for` then read
    `ROOT / target.path`, so explaining a Go target tried to open a Go file inside this
    repository and raised. `plan` took no bed at all and listed OXN's own functions under
    `--bed go-kit` -- a wrong answer rather than a crash, which is the worse of the two.

    Both were found by running the command, not by reading it. This runs it.
    """
    # `BEDS` rather than `bed()`: this is about where targets come from, which holds for a
    # bed that cannot currently run its checks just as much as for one that can.
    found = harness.BEDS[bed_name]
    picked = harness.select_targets(ceiling=3, limit=2, skip=set(), where=found)
    assert picked, f"{bed_name}: nothing over the ceiling, so this proves nothing"
    for target in picked:
        assert target.path.endswith(suffix), f"{bed_name} returned {target.path}"
        assert (found.root / target.path).is_file(), "the path must exist inside the bed"
        assert target.trail, "an explanation read from the wrong root would have raised"


def test_two_arms_are_given_the_same_targets(harness) -> None:
    """The property every number in the arm table rests on, and it did not hold.

    `oxn metrics` ranks by value and leaves equal scores in the order it found them, so
    taking the first `limit` rows returned a different target on consecutive runs -- three
    calls to `plan --limit 1` gave `topological_order`, `_merge_split_classes` and
    `tsconfig_aliases`, all scoring 12.

    A pilot ran the control and the treatment on that, and its table read "none 100%,
    hybrid 0%" because the two arms had been handed different problems. That is an artifact
    presented as a finding, which is worse than no finding, and it is the reason the tie-break
    is asserted rather than left to whatever the query happens to return.
    """
    runs = [
        [target.qualified_name for target in harness.select_targets(6, 5, skip=set())]
        for _ in range(3)
    ]
    assert runs[0] == runs[1] == runs[2], f"target order is unstable: {runs}"
    assert len(runs[0]) == len(set(runs[0])), "a target must not be offered twice"


def test_targets_are_ordered_worst_first_then_by_name(harness) -> None:
    """Worst-first is what the harness is for; the name is only how ties are settled."""
    picked = harness.select_targets(6, 8, skip=set())
    scored = [(-target.score, target.qualified_name) for target in picked]
    assert scored == sorted(scored), f"not worst-first with a stable tie-break: {scored}"


# ---- beds that could not verify their own untouched trees ---------------------------
#
# Every external bed failed this way before 2026-09-18, each differently and none of them
# for anything a repair does: httpx installed nothing into its venv, go-kit could not link
# `sd/eureka` and had a flaky `metrics` subtree, nest's pinned dependencies do not install,
# and ripgrep's `-D warnings` promoted a newer clippy's style lints into failures. A bed in
# that state scores `tests FAIL` on every attempt of every arm and renders as a table.


def test_the_go_bed_names_out_the_packages_that_fail_on_their_own_tree() -> None:
    """A bed that cannot verify its pristine tree cannot verify a repair.

    Three go-kit packages fail before anything is repaired, for two different reasons.
    `sd/eureka`'s *test binary* will not link -- the corpus pins a `golang.org/x/net` whose
    `internal/socket` still references unexported `syscall.recvmsg`, which Go 1.26's linker
    rejects. And the whole of `metrics` is flaky: four runs of `./metrics/...` on the
    untouched tree failed in `cloudwatch`, `prometheus` and `dogstatsd`, three runs out of
    four, a different subset each time.

    The flake is the worse one. A deterministic failure blocks the bed and is noticed; a
    coin-flip one is recorded as repairs that sometimes break Go and sometimes do not, which
    is indistinguishable in the log from a real regression.
    """
    import sys

    sys.path.insert(0, "scripts")
    from beds import bed

    packages = bed("go-kit").verify[0]
    assert not any("eureka" in argument for argument in packages)
    assert not any("metrics" in argument for argument in packages)
    assert "./..." not in packages, "`./...` readmits them; the list is the exclusion"


def test_the_go_bed_stops_selecting_targets_in_packages_it_stopped_testing() -> None:
    """The exclusions have to move together, and this is the one that is not tidiness.

    Dropping `metrics` from the test command while still selecting targets inside it would
    verify a repair with a suite that no longer covers it -- a green gauntlet over code
    nothing ran. That is worse than leaving those seven targets unrepaired, which is what
    dropping `metrics` from `sources` does instead.
    """
    import sys

    sys.path.insert(0, "scripts")
    from beds import bed
    from dogfood import select_targets

    go_kit = bed("go-kit")
    assert not any(source.startswith("metrics") for source in go_kit.sources)
    chosen = select_targets(12, 500, skip=set(), where=go_kit)
    assert chosen, "the bed should still offer targets outside metrics"
    assert not any(target.path.startswith("metrics/") for target in chosen)


def test_the_go_vet_command_was_narrowed_when_the_test_command_was() -> None:
    """Narrowing one verification command and leaving the other at `./...` is the easy miss.

    `go vet ./...` fails on the untouched tree for a third reason: eight diagnostics, all in
    `_test.go` files -- `t.Fatal` from a non-test goroutine in `transport/nats` and
    `sd/consul`, `t.Errorf` copying a protobuf lock value in `transport/http/proto`. Newer
    vet checks meeting older test code, and `go vet -tests=false` does not suppress them.
    """
    import sys

    sys.path.insert(0, "scripts")
    from beds import bed

    lint = bed("go-kit").verify[1]
    assert "./..." not in lint
    for excluded in ("./transport/nats/...", "./sd/consul/...", "./transport/http/proto/..."):
        assert excluded not in lint, f"{excluded} fails vet on the pristine tree"


def test_a_bed_that_declares_verification_it_cannot_run_is_still_refused() -> None:
    """"No verification declared" and "declared but unrunnable here" used to be one state.

    typescript-nest's `verify` commands are correct -- `npm test`, `npm run lint` -- and the
    bed still cannot run, because the pinned checkout's dependencies do not install: the
    committed lock file is out of sync with `package.json`, so `npm ci` refuses, and `npm
    install` abandons the pin only to hit a real peer conflict between `@apollo/server` and
    the `graphql` major `@nestjs/apollo` admits. The refusal keyed on empty `verify`, which
    this bed does not have, so it would have been handed 60 targets and failed all of them
    on a missing `vitest`.
    """
    import sys

    import pytest

    sys.path.insert(0, "scripts")
    from beds import bed

    with pytest.raises(SystemExit, match="cannot run it here"):
        bed("typescript-nest")

    for still_fine in ("go-kit", "python-httpx"):
        bed(still_fine)


def test_the_rust_bed_does_not_promote_a_newer_clippy_into_a_failure() -> None:
    """`cargo clippy -- -D warnings` failed on ripgrep's untouched tree, so the bed was inert.

    Unlike go-kit's two named packages, the cause is not localised: clippy 1.98 against
    ripgrep's pinned code reports a dozen distinct *style* lints across crates --
    `unnecessary_map_or`, `new_without_default`, `collapsible_if`, `needless_borrow`,
    `needless_lifetimes` and more. Each is a suggestion a later clippy added, none is a defect
    a repair introduced, and naming them out would be a list that grows with clippy's release
    cadence rather than with this corpus.

    Dropping the promotion keeps the check and loses the hostage: `cargo clippy` still fails
    on a real compile error, which is what "did the repair break this" asks. Pinned because
    `-D warnings` is the reflexive thing to write and it makes this bed unusable.
    """
    import sys

    sys.path.insert(0, "scripts")
    from beds import bed

    lint = bed("rust-ripgrep").verify[1]
    assert lint[:3] == ("lint", "cargo", "clippy")
    assert "-D" not in lint and "warnings" not in lint, (
        "`-D warnings` fails on the pristine tree; see this test's docstring"
    )
