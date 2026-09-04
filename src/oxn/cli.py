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

    from oxn.check import CheckReport, run_check  # noqa: PLC0415 -- lazy; see the docstring
    from oxn.config import ConfigError  # noqa: PLC0415

    targets = _hook_targets(args.paths)
    if targets is None:
        json.dump(CheckReport(scope="files").as_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    try:
        report = run_check(targets, deep=args.deep, use_baseline=not args.no_baseline)
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


def _hook_targets(paths: list[str]) -> list[str] | None:
    """What ``oxn check --json`` should measure, or ``None`` for "nothing to measure".

    Explicit arguments win. With none, stdin decides, because the two callers that pass no
    path want opposite things:

    * A **PostToolUse hook** sends a JSON payload naming the file the agent just edited.
      That file is the entire job. Checking the repository instead is what this function
      exists to stop: measured on OXN's own tree with the benchmark corpora fetched, the
      whole-tree walk cost **7.27 s per edit** against ADR-0002's 200 ms budget, while the
      one named file costs 67 ms. `tests/test_latency.py` never caught it because it passed
      a path -- it measured a route the deployed hook does not take.
    * A **human or CI** runs it with stdin empty or a terminal, and means the whole tree.

    A payload that names nothing analysable returns ``None`` rather than falling back to the
    tree. The fallback is the trap: the matcher can be widened to a tool that edits no file
    (or the payload shape can change), and a tree-walking fallback would quietly reinstate
    the seven seconds with nothing to show for it.
    """
    if paths:
        return paths
    payload = _stdin_payload()
    if payload is None:
        return ["."]
    edited = _edited_path(payload)
    # A file the edit deleted or moved is not a violation, and reporting it as a missing
    # path would fail the hook on a legitimate edit.
    return [edited] if edited and _exists(edited) else None


def _stdin_payload() -> dict[str, object] | None:
    """The hook payload on stdin, or ``None`` when this was not invoked by a hook."""
    import json

    if sys.stdin is None or sys.stdin.isatty():
        return None
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):  # closed, or not readable in this context
        return None
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


#: Where the file-editing tools put the path they wrote. `file_path` is the documented key
#: for Edit, Write and MultiEdit; `notebook_path` is NotebookEdit's.
PATH_KEYS = ("file_path", "notebook_path")


def _edited_path(payload: dict[str, object]) -> str | None:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    for key in PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _exists(path: str) -> bool:
    import os.path

    return os.path.exists(path)


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
