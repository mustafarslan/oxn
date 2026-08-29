"""The latency budget is a promise in ADR-0002. Assert it, do not hope for it.

Thresholds here are deliberately generous relative to the design target (p95 <= 200 ms,
400 ms hard ceiling) because CI runners are slower and noisier than a laptop. The point is
to catch a *regression in kind* -- someone adding a heavy top-level import -- not to
benchmark. The precise measurement lives in the P2 performance lane.
"""

from __future__ import annotations

import subprocess
import sys
import time

HARD_CEILING_S = 0.400


def _time_cold_run(*args: str) -> float:
    """Best of five cold subprocess runs, in seconds."""
    best = float("inf")
    for _ in range(5):
        start = time.perf_counter()
        subprocess.run(
            [sys.executable, "-m", "oxn.cli", *args],
            capture_output=True,
            check=True,
        )
        best = min(best, time.perf_counter() - start)
    return best


def test_fast_path_stays_inside_the_hard_ceiling() -> None:
    elapsed = _time_cold_run("check", "--json")
    assert elapsed < HARD_CEILING_S, (
        f"`oxn check --json` took {elapsed * 1000:.0f} ms cold, over the "
        f"{HARD_CEILING_S * 1000:.0f} ms hard ceiling. Something heavy reached module level."
    )
