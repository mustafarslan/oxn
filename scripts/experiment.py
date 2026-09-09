"""The grid: every arm over one bed's targets, run once so the flags cannot drift.

The harness runs *one* configuration per invocation, and P11's table needs twelve. Doing that
by hand is twelve chances to vary something that was supposed to be held constant, and the
pilots showed what that costs: the first arm table read "none 100%, hybrid 0%" because the
two invocations had silently been handed different targets.

So the arms are chosen once, the targets are chosen once, and every arm gets the same list.
That is the whole reason this exists -- not convenience, but the single property an arm table
depends on and cannot assert about itself.

**It is deliberately not parallel.** Running arms concurrently is the obvious way to finish
sooner and it is a decision with two costs a comparison cannot absorb: sandboxes building
virtualenvs at the same time contend for the same disk and CPU, so `seconds` stops being a
measure of the arm, and a machine at full load for hours is not a thing to start on someone
else's behalf. `--arm` on the harness remains the way to run one configuration at a time.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from arms import arm_names
from beds import bed
from dogfood import Session, repair
from targets import select_targets

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def say(message: str = "") -> None:
    print(message)


@dataclass(frozen=True, slots=True)
class Grid:
    """One experiment: which arms, over which bed, at what depth."""

    bed: str
    arms: tuple[str, ...]
    ceiling: int
    limit: int
    retries: int
    repeats: int
    backend: str

    @property
    def runs(self) -> int:
        """Configurations, not attempts. Attempts are `runs * limit * repeats` at least."""
        return len(self.arms)


def run(grid: Grid) -> int:
    """Every arm over the same targets, in order, reporting as it goes.

    Targets are resolved **once** and passed to each arm. `select_targets` is deterministic
    now, so re-resolving would give the same answer -- and relying on that would make this
    depend on a property it does not enforce, which is how the first pilot went wrong.
    """
    where = bed(grid.bed)
    targets = select_targets(grid.ceiling, grid.limit, skip=set(), where=where)
    if not targets:
        say(f"nothing over {grid.ceiling} in {grid.bed}")
        return 0

    say(f"{BOLD}{grid.bed}{RESET}: {len(targets)} target(s) x {len(grid.arms)} arm(s)")
    for target in targets:
        say(f"  {DIM}{target.score:.0f}  {target.qualified_name}{RESET}")

    for name in grid.arms:
        say(f"\n{BOLD}=== arm {name} ==={RESET}")
        repair(list(targets), _session(grid, name), where)
    return 0


def _session(grid: Grid, name: str) -> Session:
    """One arm's session. Everything except the arm is held identical by construction."""
    return Session(
        ceiling=grid.ceiling,
        retries=grid.retries,
        backend=grid.backend,
        arm=name,
        repeats=grid.repeats,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--bed", default="self")
    parser.add_argument(
        "--arms",
        default="none,hybrid",
        help=f"comma-separated, from: {', '.join(arm_names())}. `none` is the control",
    )
    parser.add_argument("--ceiling", type=int, default=12)
    parser.add_argument("--limit", type=int, default=3, help="targets, shared by every arm")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--repeat", type=int, default=1, help="samples per (arm, target)")
    parser.add_argument("--backend", default="ollama")
    parser.add_argument("--dry-run", action="store_true", help="print the grid and stop")
    args = parser.parse_args(argv)

    grid = Grid(
        bed=args.bed,
        arms=tuple(name.strip() for name in args.arms.split(",") if name.strip()),
        ceiling=args.ceiling,
        limit=args.limit,
        retries=args.retries,
        repeats=args.repeat,
        backend=args.backend,
    )
    if args.dry_run:
        say(f"{grid.runs} configuration(s): {', '.join(grid.arms)} on {grid.bed}")
        say(f"up to {grid.runs * grid.limit * grid.repeats * grid.retries} attempts")
        return 0
    return run(grid)


if __name__ == "__main__":
    raise SystemExit(main())
