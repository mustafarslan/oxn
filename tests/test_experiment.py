"""The grid driver: every arm over the same targets, chosen once.

The harness runs one configuration per invocation and P11's table needs twelve. Doing that by
hand is twelve chances to vary something meant to be held constant, and the first pilot showed
the cost precisely: its table read "none 100%, hybrid 0%" because two invocations had been
handed different targets. Nothing in the output said so.

So the targets are resolved once and handed to every arm. `select_targets` is deterministic
now and re-resolving would give the same answer -- but relying on that would make the driver
depend on a property it does not itself enforce, which is exactly how the pilot went wrong.
"""

from __future__ import annotations

import pytest
from tests.test_dogfood import load_harness


@pytest.fixture(scope="module")
def harness():
    return load_harness()


@pytest.fixture
def module(harness):
    """The `experiment` module itself, not the merged namespace.

    `run` reads its own globals, so patching an attribute on the merged namespace changes
    nothing it can see -- the first version of these tests did exactly that and passed for
    the wrong reason until the target list came back empty and gave it away.
    """
    import sys

    del harness
    return sys.modules["experiment"]


@pytest.fixture
def grid(harness):
    def build(**over):
        fields = {
            "bed": "self",
            "arms": ("none", "hybrid"),
            "ceiling": 6,
            "limit": 3,
            "retries": 3,
            "repeats": 1,
            "backend": "dry-run",
        }
        return harness.Grid(**{**fields, **over})

    return build


def test_every_arm_is_handed_the_same_target_list(module, grid, monkeypatch) -> None:
    """The one property an arm table depends on and cannot assert about itself."""
    seen: list[list[str]] = []
    monkeypatch.setattr(
        module,
        "repair",
        lambda targets, session, where: seen.append([t.qualified_name for t in targets]),
    )
    module.run(grid(arms=("none", "mcp", "hybrid"), limit=2))
    assert len(seen) == 3, "one call per arm"
    assert seen[0] == seen[1] == seen[2], f"arms got different targets: {seen}"
    assert seen[0], "and the list must not be empty, or this proves nothing"


def test_targets_are_resolved_once_not_per_arm(module, grid, monkeypatch) -> None:
    """Re-resolving would depend on determinism the driver does not enforce itself."""
    calls = {"n": 0}
    original = module.select_targets

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "select_targets", counted)
    monkeypatch.setattr(module, "repair", lambda *a, **k: None)
    module.run(grid(arms=("none", "mcp", "hybrid")))
    assert calls["n"] == 1, f"resolved {calls['n']} times; every arm must share one list"


def test_only_the_arm_differs_between_configurations(harness, grid) -> None:
    """Everything else is held identical by construction rather than by care."""
    plan = grid(arms=("none", "hybrid"), repeats=2, retries=4)
    sessions = [harness._session(plan, name) for name in plan.arms]
    varying = {
        field
        for field in ("ceiling", "retries", "backend", "repeats")
        if len({getattr(session, field) for session in sessions}) > 1
    }
    assert not varying, f"these differ between arms and must not: {sorted(varying)}"
    assert [session.arm for session in sessions] == ["none", "hybrid"]


def test_a_fake_actor_never_writes_to_the_benchmark_record(harness) -> None:
    """`--dry-run` and `--backend dry-run` reach the same fake, and only one suppressed logs.

    So a grid run with the fake backend wrote fabricated attempts into
    `benchmarks/dogfood-log.jsonl` beside measured ones, where nothing downstream could tell
    them apart. A record mixing invented rows with real ones is worse than an empty one.
    """
    assert harness.Session(ceiling=12, retries=1, dry_run=True).is_fake
    assert harness.Session(ceiling=12, retries=1, backend="dry-run").is_fake
    assert not harness.Session(ceiling=12, retries=1, backend="ollama").is_fake


def test_the_dry_grid_prices_the_run_before_it_starts(harness, grid) -> None:
    """A six-arm grid is a multi-hour commitment, and the operator should see that first."""
    plan = grid(arms=harness.arm_names(), limit=3, repeats=3, retries=3)
    assert plan.runs == 6
    assert plan.runs * plan.limit * plan.repeats * plan.retries == 162
