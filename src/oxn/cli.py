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
from typing import Any, NoReturn

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
    from oxn.config import Config, ConfigError  # noqa: PLC0415

    targets, session = _hook_request(args.paths)
    if targets is None:
        _emit(CheckReport(scope="files"))
        return 0

    try:
        settings = Config.load()
        report = run_check(
            targets, config=settings, deep=args.deep, use_baseline=not args.no_baseline
        )
    except ConfigError as error:
        json.dump(
            {"status": "ERROR", "errors": {"oxn.yaml": str(error)}, "violations": []},
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        sys.stderr.write(f"OXN could not read oxn.yaml, so nothing was checked: {error}\n")
        return 1

    if session:
        _charge_retries(report, settings, session)
    _emit(report)
    return report.exit_code


def _emit(report: Any) -> None:
    """Write the report to both streams, because the two readers are different.

    **stdout** carries the JSON: CI parses it, `oxn baseline` and the MCP server's
    `check_code` consume the same shape, and `tests/test_cli.py` asserts it is always there.

    **stderr** carries the prose, and it is not a duplicate. Claude Code's `PostToolUse`
    contract shows *stderr* to the agent on exit 2 and puts stdout in the transcript, so for
    the whole of P9 a rejected edit reached Claude as `No stderr output` — the gate blocking
    correctly and explaining nothing. Silence on a passing check is deliberate: a hook that
    speaks when nothing is wrong is a hook people stop reading.
    """
    import json  # noqa: PLC0415 -- lazy; see the module docstring

    from oxn.check import remediation  # noqa: PLC0415

    json.dump(report.as_dict(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    if report.exit_code != 0:
        sys.stderr.write(remediation(report) + "\n")


def _charge_retries(report: Any, settings: Any, session: str) -> None:
    """Count this run against the session's budget, and record where each violation stands.

    Every path this run *measured* is passed down, not just the failing ones: a file that
    came back clean is how the ledger learns a violation was repaired, and omitting it would
    leave the count standing until the session ended.
    """
    if settings.retry_budget <= 0:
        return
    from oxn.retry import charge, ledger_path  # noqa: PLC0415 -- lazy; see the docstring

    measured: dict[str, dict[str, float]] = {path: {} for path in report.paths}
    for finding in report.failing:
        measured.setdefault(finding.path, {})[finding.key] = finding.value
    report.attempts = charge(ledger_path(settings.root, session), measured)
    report.retry_budget = settings.retry_budget


def _hook_request(paths: list[str]) -> tuple[list[str] | None, str]:
    """What to measure, and which agent session asked — the payload is read exactly once.

    The session id is what makes a retry budget possible at all: `PostToolUse` fires per
    edit and remembers nothing, so "this is the third time I have told you about this
    function" is a claim only a ledger keyed by session can make. It is absent whenever OXN
    is run by a person or by CI, and the budget is then not applied — halting a build after
    three commits touched the same debt would be nonsense.
    """
    if paths:
        return paths, ""
    payload = _stdin_payload()
    if payload is None:
        return ["."], ""
    session = payload.get("session_id")
    return _edited_targets(payload), session if isinstance(session, str) else ""


def _edited_targets(payload: dict[str, object]) -> list[str] | None:
    """What ``oxn check --json`` should measure, or ``None`` for "nothing to measure".

    A **PostToolUse hook** sends a JSON payload naming the file the agent just edited. That
    file is the entire job. Checking the repository instead is what this function exists to
    stop: measured on OXN's own tree with the benchmark corpora fetched, the whole-tree walk
    cost **7.27 s per edit** against ADR-0002's 200 ms budget, while the one named file
    costs 67 ms. `tests/test_latency.py` never caught it because it passed a path -- it
    measured a route the deployed hook does not take. (A **human or CI** runs OXN with stdin
    empty or a terminal and means the whole tree, which `_hook_request` answers above,
    before there is a payload to read.)

    A payload that names nothing analysable returns ``None`` rather than falling back to the
    tree. The fallback is the trap: the matcher can be widened to a tool that edits no file
    (or the payload shape can change), and a tree-walking fallback would quietly reinstate
    the seven seconds with nothing to show for it.
    """
    edited = _edited_path(payload)
    # A file the edit deleted or moved is not a violation, and reporting it as a missing
    # path would fail the hook on a legitimate edit.
    return [edited] if edited and _exists(edited) else None


#: How long to let a hook finish writing its payload. Only ever paid when nothing arrives,
#: because `select` returns the moment there is something to read.
PAYLOAD_WAIT_S = 0.25


def _stdin_payload() -> dict[str, object] | None:
    """The hook payload on stdin, or ``None`` when this was not invoked by a hook."""
    import json

    raw = _read_available()
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _read_available() -> str:
    """Whatever is on stdin now, giving a hook a moment to write, and never blocking.

    "Nothing to read" and "a payload that has not been written yet" are the same thing to a
    file descriptor, so this waits briefly and then gives up rather than asking for EOF. A
    plain ``sys.stdin.read()`` here is a deadlock, and not hypothetically: `oxn check --deep`
    inside an ordinary shell pipeline inherits a pipe nobody ever writes to, and the first
    version of this function hung OXN's own gate until it was killed.

    The loop is bounded at both ends -- EOF, or a quiet quarter second -- so a writer that
    sends half a payload and keeps the pipe open cannot hang it either.

    Where ``select`` cannot watch a pipe, which means Windows, nothing is read and the caller
    falls back to checking the tree. That is what OXN did before it read payloads at all;
    hanging is the one outcome that is not acceptable.
    """
    import os
    import select

    if sys.stdin is None or sys.stdin.isatty():
        return ""
    chunks: list[bytes] = []
    try:
        while select.select([sys.stdin], [], [], PAYLOAD_WAIT_S)[0]:
            chunk = os.read(sys.stdin.fileno(), 65536)
            if not chunk:
                break
            chunks.append(chunk)
    except (OSError, ValueError):  # closed, unwatchable, or not a real descriptor
        return ""
    return b"".join(chunks).decode("utf-8", "replace")


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
