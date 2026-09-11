"""The MCP stdio server: OXN's read surface for a coding agent.

[ADR-0003](../../docs/adr/0003-enforcement-model.md) splits OXN in two -- the hook enforces
and MCP informs -- and this module is the second half's transport.
[ADR-0004](../../docs/adr/0004-freshness-model.md) makes it the warm process: it is alive
for the whole agent session anyway, so the freshness model never needs a daemon.

**The protocol is hand-written, and that is a decision rather than an omission.**
[ADR-0001](../../docs/adr/0001-dependency-policy.md), amended 2026-09-05, admits the MCP
Python SDK at runtime *in principle* and declines it here on a measurement: it installs 28
packages and ~39 MB, including `cryptography` and `cffi` -- compiled wheels, pulled in for
an OAuth flow a stdio server never performs. OXN's wedge is one `pip install` with no
toolchain, and a compiled wheel is exactly what breaks that on a new Python. The SDK is a
**CI oracle** instead (`tests/test_oracle_mcp.py`), which is what this project already does
with every analyser it chose not to depend on: the conformance claim is checked by the real
client rather than asserted.

**Stdout is the wire.** Anything that prints -- a `rich` console, a lazily fetched grammar,
a stray `print` -- corrupts the stream and the client sees a protocol error rather than the
bug. `serve` therefore takes the real stdout away from the process and points `sys.stdout`
at stderr for the server's lifetime, so a stray write is merely noisy.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable, Iterable, Iterator

#: The protocol revision this server is written against. A client asking for a version we
#: do not know is answered with this one rather than refused -- the spec's own guidance,
#: and the alternative is a hard failure over a field neither side uses yet.
PROTOCOL_VERSION = "2025-06-18"
KNOWN_PROTOCOLS = frozenset({"2024-11-05", "2025-03-26", PROTOCOL_VERSION})

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class ProtocolError(Exception):
    """A transport-level fault: the message was wrong, not the tool.

    A *tool* that fails is not this. It returns `isError: true` with the reason as text,
    because the agent is supposed to read it and try something else -- while a JSON-RPC
    error is for the client's plumbing and never reaches the model.
    """

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---- the tools ---------------------------------------------------------------------------


class ContextRequest(BaseModel):
    """Arguments to `get_architectural_context`."""

    task: str = Field(description="What you are about to do, in your own words.")
    files: list[str] = Field(
        default_factory=list,
        description="Files the task is about. Scope beats wording: a constraint governing "
        "one of these outranks a better-worded one that governs nothing here.",
    )
    limit: int = Field(default=0, description="Constraints to return; 0 uses OXN's budget.")


class CheckRequest(BaseModel):
    """Arguments to `check_code`."""

    paths: list[str] = Field(
        default_factory=list, description="Files or directories to check. Empty means the tree."
    )
    deep: bool = Field(
        default=False,
        description="Also check repository-scoped contracts (layering, cycles). Slower.",
    )


def _architectural_context(request: ContextRequest) -> dict[str, Any]:
    from oxn import thresholds
    from oxn.config import Config
    from oxn.context.bundle import build_bundle
    from oxn.context.project import load_project

    bundle = build_bundle(
        load_project(Config.load()),
        task=request.task,
        targets=request.files,
        limit=request.limit or thresholds.MAX_BUNDLE_CONSTRAINTS,
    )
    return bundle.model_dump()


def _check_code(request: CheckRequest) -> dict[str, Any]:
    from oxn.check import run_check

    return run_check(list(request.paths) or ["."], deep=request.deep).as_dict()


class MetricsRequest(BaseModel):
    """Arguments to `get_metrics`."""

    paths: list[str] = Field(description="Files or directories to measure.")
    sort_by: str = Field(
        default="cognitive_complexity", description="The metric to rank entities by."
    )
    limit: int = Field(default=20, description="How many entities to return.")
    explain: bool = Field(
        default=False,
        description="Include the increment trail for the worst entity: what scored, where.",
    )


class ExplainRequest(BaseModel):
    """Arguments to `explain_violation` -- a finding's identity, as `check_code` reported it."""

    path: str = Field(description="The `path` of the finding.")
    entity: str = Field(description="The `entity` of the finding: its qualified name.")
    rule: str = Field(description="The `rule` of the finding, e.g. `cognitive_complexity`.")


def _get_metrics(request: MetricsRequest) -> dict[str, Any]:
    from oxn.report import metrics_payload

    return metrics_payload(
        list(request.paths),
        sort_by=request.sort_by,
        limit=request.limit,
        explain=request.explain,
    )


def _explain_violation(request: ExplainRequest) -> dict[str, Any]:
    """Re-measure one file, find that finding again, and say where its ceiling came from.

    Re-measuring rather than trusting the arguments is the point: between `check_code` and
    this call the agent has probably edited the file, and "that is no longer a violation" is
    a better answer than an explanation of something that is no longer true.
    """
    from oxn.check import run_check
    from oxn.config import Config
    from oxn.context.bundle import declarations_of
    from oxn.context.project import load_project

    report = run_check([request.path])
    key = f"{request.rule}|{request.path}|{request.entity}"
    every = [*report.findings, *report.baselined, *report.regressed]
    found = next((finding for finding in every if finding.key == key), None)
    declared = declarations_of(load_project(Config.load()), request.rule, request.path)
    return {
        "status": "resolved" if found is None else "violation",
        "key": key,
        "finding": found.as_dict() if found else None,
        "increments": list(found.explanation) if found else [],
        # `relevance` is excluded rather than emitted as zero: there is no task text to rank
        # against here, and a score of 0.0 reads as "irrelevant" rather than "not asked".
        "declared_by": [constraint.model_dump(exclude={"relevance"}) for constraint in declared],
    }


@dataclass(frozen=True, slots=True)
class Tool:
    """One callable surface, with the schema the client validates against."""

    name: str
    description: str
    request: type[BaseModel]
    run: Callable[[Any], dict[str, Any]]

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.request.model_json_schema(),
        }


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="get_architectural_context",
        description=(
            "The constraints that govern a task -- ceilings, architectural contracts and "
            "recorded decisions -- ranked against what you are doing and capped to a budget. "
            "Call this BEFORE writing code, not after being rejected. It can never pass or "
            "fail anything: the gate enforces every constraint, shown here or not. "
            "`ungoverned` names the files you asked about that no contract's layer rules "
            "reach -- not a violation, the opposite: nothing constrains their imports, and "
            "the gate cannot tell you so because having no rule produces no finding."
        ),
        request=ContextRequest,
        run=_architectural_context,
    ),
    Tool(
        name="check_code",
        description=(
            "Run OXN's gate over files or directories and return what it found: blocking "
            "violations, regressions against the accepted baseline, and advisory findings "
            "that are reported but never block. This is the same verdict the PostToolUse "
            "hook reaches, so an edit that passes here passes there."
        ),
        request=CheckRequest,
        run=_check_code,
    ),
    Tool(
        name="get_metrics",
        description=(
            "Measure files and rank their functions, classes and modules by one metric -- "
            "cognitive complexity, nesting, length, coupling. This reports; it never judges. "
            "Use it to find where the risk is before you touch anything, or to see how far "
            "under a ceiling something sits."
        ),
        request=MetricsRequest,
        run=_get_metrics,
    ),
    Tool(
        name="explain_violation",
        description=(
            "Why one finding from `check_code` is a violation: the increment trail -- what "
            "scored, at which line, and why -- and where its ceiling was declared, from the "
            "project default through any layer override to a decision record that tightened "
            "it. The file is re-measured, so a violation you have already fixed comes back "
            "as `resolved` rather than as an explanation of something no longer true."
        ),
        request=ExplainRequest,
        run=_explain_violation,
    ),
)


# ---- the protocol ------------------------------------------------------------------------


class Session:
    """One client connection, for as long as the process lives."""

    def __init__(self, tools: Iterable[Tool] = TOOLS) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._methods: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "initialize": self._initialize,
            "ping": lambda _params: {},
            "tools/list": lambda _params: {
                "tools": [tool.describe() for tool in self._tools.values()]
            },
            "tools/call": self._call,
        }

    def respond(self, message: object) -> dict[str, Any] | None:
        """Answer one decoded message, or `None` when there is nothing to answer.

        A message with no `id` is a **notification** -- `notifications/initialized` arrives
        immediately after the handshake -- and answering one puts traffic on an id the client
        never issued. The official SDK's client tolerates that (it drops the frame), which is
        measured, not assumed: `tests/test_oracle_mcp.py` stays green with this guard removed.
        The rule is the specification's, so it is asserted at the frame level instead.
        """
        if not isinstance(message, dict):
            # A JSON array here is a batch, which this revision of the spec removed.
            return _error(None, INVALID_REQUEST, "expected one JSON-RPC object per line")
        if "id" not in message:
            return None
        identifier = message["id"]
        handler = self._methods.get(str(message.get("method", "")))
        if handler is None:
            return _error(identifier, METHOD_NOT_FOUND, f"unknown method {message.get('method')!r}")
        try:
            result = handler(_params(message))
        except ProtocolError as error:
            return _error(identifier, error.code, error.message)
        return {"jsonrpc": "2.0", "id": identifier, "result": result}

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        from oxn import __version__

        asked = str(params.get("protocolVersion", ""))
        return {
            "protocolVersion": asked if asked in KNOWN_PROTOCOLS else PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "oxn", "version": __version__},
        }

    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name", ""))
        tool = self._tools.get(name)
        if tool is None:
            raise ProtocolError(INVALID_PARAMS, f"no such tool: {name!r}")
        try:
            request = tool.request.model_validate(params.get("arguments") or {})
        except ValidationError as error:
            return _failed(f"{name}: arguments rejected\n{error}")
        return _ran(tool, request)


def _ran(tool: Tool, request: BaseModel) -> dict[str, Any]:
    """Run one tool. Its failure is content for the agent, never a transport error.

    The broad except is the point rather than a lapse: OXN analyses whatever tree it is
    pointed at, and a malformed `oxn.yaml`, an unreadable file or a grammar that will not
    load must reach the agent as something it can act on. A JSON-RPC error would reach the
    client's plumbing and show the model nothing.
    """
    try:
        payload = tool.run(request)
    except Exception as error:  # noqa: BLE001 -- see the docstring
        return _failed(f"{tool.name} failed: {type(error).__name__}: {error}{_staleness()}")
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}],
        "structuredContent": payload,
        "isError": False,
    }


def _failed(reason: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": reason}], "isError": True}


def _newest_source() -> float:
    """The most recent mtime under the installed package, or 0.0 if it cannot be read."""
    try:
        return max(
            (path.stat().st_mtime for path in Path(__file__).resolve().parent.rglob("*.py")),
            default=0.0,
        )
    except OSError:
        return 0.0


#: What the package looked like when this process imported it. ADR-0004 makes the server the
#: warm path *for the index*, and it refreshes that incrementally -- but nothing refreshes the
#: server's own code, so a process outlives the OXN it is running. On a project that gates
#: itself that is the normal case, not an edge one.
_SOURCE_STAMP = _newest_source()


def _staleness() -> str:
    """Why a tool may have failed for a reason that is not in this repository.

    Found by being on the receiving end: `get_architectural_context` answered
    ``ConfigError: 'methods_per_class' is not a gated rule`` against an `oxn.yaml` that
    declares exactly that, because the server had been running since before the rule existed.
    The error was true of the code the process holds and false of the code on disk, and
    nothing in it said which -- an unactionable message from the tool whose whole job is to
    be actionable.
    """
    if _SOURCE_STAMP == 0.0 or _newest_source() <= _SOURCE_STAMP:
        return ""
    return (
        "\n\nNOTE: this `oxn serve` process imported OXN before the code on disk was last"
        " changed, so it may be answering from an older version -- including about rules"
        " `oxn.yaml` names and this build does not have. `oxn check` from the shell always"
        " runs the current code, and is the way to get an answer now. Fixing the server"
        " means starting a new client session: killing this process does not bring a fresh"
        " one back, it removes the tools for the rest of the session."
    )


def _params(message: dict[str, Any]) -> dict[str, Any]:
    params = message.get("params")
    return params if isinstance(params, dict) else {}


def _error(identifier: object, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


def serve(
    lines: Iterator[str] | None = None,
    out: Any = None,
    session: Session | None = None,
    root: str | None = None,
) -> None:
    """Read newline-delimited JSON-RPC until the client hangs up.

    Takes stdout away from the rest of the process first, so that anything which prints --
    a console, a lazily fetched tree-sitter grammar -- lands on stderr instead of corrupting
    the wire. That is not defensive: `_console()` writes to stdout, and every OXN surface
    reachable from here is one import away from it.

    Then moves to the project root, because an editor starts an MCP server in whatever
    directory it likes and the tools disagree about what that means. `Config.load` walks up
    to `oxn.yaml`, so `get_architectural_context` was already answering about the whole
    repository -- while `check_code(paths=["src/oxn/server.py"])` resolved that path against
    the *client's* cwd and answered "no such file or directory". One tool right and one
    silently wrong is the broken-editor-tool failure this phase exists to avoid; every path
    the server takes or returns is now repository-relative.
    """
    stream = out or sys.stdout
    if out is None:
        sys.stdout = sys.stderr
    os.chdir(_root(root))
    live = session or Session()
    for line in lines if lines is not None else sys.stdin:
        if not line.strip():
            continue
        response = _answer(live, line)
        if response is None:
            continue
        stream.write(json.dumps(response, separators=(",", ":")) + "\n")
        stream.flush()


def _root(root: str | None) -> Path:
    """Where to serve from: `--root`, else `OXN_ROOT`, else the project the cwd is inside.

    The environment variable exists because an MCP client configures a *command*, and the
    editors that let you set one often let you set the environment beside it and nothing
    else. With none of the three, discovery is `Config.load`'s: the nearest `oxn.yaml` at or
    above the working directory, bounded by the repository.
    """
    chosen = root or os.environ.get("OXN_ROOT")
    from oxn.config import Config

    resolved = Config.load(Path(chosen) if chosen else None).root
    print(f"oxn: serving {resolved}", file=sys.stderr, flush=True)
    return resolved


def _answer(session: Session, line: str) -> dict[str, Any] | None:
    try:
        message = json.loads(line)
    except json.JSONDecodeError as error:
        return _error(None, PARSE_ERROR, f"not JSON: {error}")
    return session.respond(message)
