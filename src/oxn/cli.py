"""Command-line entry point.

Two dispatch paths, deliberately:

* The **hook fast path** (``oxn check ... --json``) runs after every agent edit and must
  stay inside the latency budget of ADR-0002 (p95 <= 200 ms). It is dispatched with the
  standard library alone. Measured on the development machine: a bare interpreter costs
  ~14 ms, ``import typer`` adds ~23 ms and ``import rich.console`` ~15 ms -- a third of
  the budget spent before any file is read.

* The **human path** (everything else) may take its time, so it uses typer and rich.

The rule this file exists to enforce: **nothing heavy at module level.** ``tests/
test_import_guard.py`` fails the build if that slips.
"""

from __future__ import annotations

import sys
from typing import NoReturn

# Modules that must never be reachable from the hook fast path. Kept here so the guard
# test and the humans reading this file see the same list.
FORBIDDEN_ON_FAST_PATH = frozenset(
    {"typer", "click", "rich", "pydantic", "networkx", "numpy", "torch", "pandas"}
)


def _is_fast_path(argv: list[str]) -> bool:
    """True for the machine-readable check the PostToolUse hook invokes."""
    return bool(argv) and argv[0] == "check" and "--json" in argv


def _run_fast_path(argv: list[str]) -> int:
    """Stdlib-only dispatch for ``oxn check --json``.

    Everything heavy is imported *inside* this function, after argument parsing, so a
    malformed invocation costs a bare interpreter rather than a full engine load. The engine
    itself is unavoidable once there is work to do -- the point of the split was never to
    avoid the analysis, only the presentation layer that the hook has no use for.
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="oxn check", add_help=True)
    parser.add_argument("paths", nargs="*", help="files or directories to check")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--deep",
        action="store_true",
        help="also check repository-scoped contracts; too slow for a hook",
    )
    parser.add_argument("--no-baseline", action="store_true", help="ignore .oxn/baseline.json")
    args = parser.parse_args(argv[1:])

    from oxn.check import run_check  # noqa: PLC0415 -- lazy by design; see the docstring
    from oxn.config import ConfigError  # noqa: PLC0415

    try:
        report = run_check(args.paths, deep=args.deep, use_baseline=not args.no_baseline)
    except ConfigError as error:
        json.dump(
            {"status": "ERROR", "errors": {"oxn.yaml": str(error)}, "violations": []},
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 1

    json.dump(report.as_dict(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return report.exit_code


def _version() -> str:
    from oxn import __version__

    return __version__


def main(argv: list[str] | None = None) -> NoReturn:
    """Entry point for the ``oxn`` script."""
    args = list(sys.argv[1:] if argv is None else argv)

    if _is_fast_path(args):
        raise SystemExit(_run_fast_path(args))

    from oxn._app import app  # noqa: PLC0415 -- deliberately lazy; see module docstring

    app()
    raise SystemExit(0)


if __name__ == "__main__":
    main()
