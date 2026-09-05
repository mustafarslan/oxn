"""The hand-written MCP server, driven by the official SDK's client.

[ADR-0001](../docs/adr/0001-dependency-policy.md), amended 2026-09-05, declines the MCP
Python SDK as a runtime dependency on a measurement -- 28 packages and ~39 MB, including
compiled `cryptography` and `cffi` wheels pulled in for an OAuth flow a stdio server never
performs -- and takes it as a **CI oracle** instead. That is the same trade this project
makes with radon, lizard, grimp and PMD: implement it, then let the tool we chose not to
depend on decide whether the implementation is right.

This is the test that makes "OXN speaks MCP" checkable. `tests/test_server.py` asserts what
the frames should look like; this asserts that a real client, doing a real handshake over a
real pipe to a real subprocess, gets what it expects. A hand-written protocol without this
is an assertion.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.oracle

ROOT = Path(__file__).resolve().parent.parent


async def _drive(calls: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Run the whole client lifecycle against `oxn serve` and report what came back."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "oxn", "serve"], cwd=str(ROOT)
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        initialized = await session.initialize()
        listed = await session.list_tools()
        results = [await session.call_tool(name, arguments) for name, arguments in calls]
        return {"initialize": initialized, "tools": listed.tools, "results": results}


def _run(calls: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    return asyncio.run(asyncio.wait_for(_drive(calls), timeout=120))


def test_the_official_client_completes_a_session_against_our_server() -> None:
    """Handshake, tool discovery and two tool calls, over a pipe to a real subprocess.

    **What this oracle cannot adjudicate, measured rather than assumed.** The obvious claim
    to make here is that answering `notifications/initialized` desynchronises the client. It
    does not: sabotaging the notification guard in `oxn.server` leaves this test green,
    because the SDK's client dispatches responses by request id and silently drops one it
    never asked for. A tolerant client is the normal case and it is exactly why a
    conformance oracle is not a superset of a frame-level test --
    `tests/test_server.py::test_a_notification_is_never_answered` is load-bearing, not a
    cheaper restatement of this file. The timeout below is a hang guard, nothing more.
    """
    outcome = _run(
        [
            ("get_architectural_context", {"task": "add a rule to the engine", "limit": 4}),
            ("check_code", {"paths": ["src/oxn/server.py"]}),
        ]
    )

    assert outcome["initialize"].server_info.name == "oxn"
    assert {(tool.name, tool.input_schema["type"]) for tool in outcome["tools"]} == {
        ("get_architectural_context", "object"),
        ("check_code", "object"),
        ("get_metrics", "object"),
        ("explain_violation", "object"),
    }

    bundle, report = outcome["results"]
    served = json.loads(bundle.content[0].text)
    assert (bundle.is_error, report.is_error) == (False, False)
    assert served["constraints"], "an empty bundle proves nothing"
    # The typed half of the answer, for clients that read it: same payload, not a summary.
    assert bundle.structured_content == served
    assert json.loads(report.content[0].text)["status"] in {"PASSED", "FAILED"}


def test_a_tool_failure_reaches_the_client_as_content_rather_than_a_transport_error() -> None:
    """The distinction the SDK can adjudicate: `isError` is a result the model reads, and a
    JSON-RPC error is plumbing it never sees. A bad path is the agent's problem to fix."""
    outcome = _run([("check_code", {"paths": ["no/such/file.py"]})])
    (result,) = outcome["results"]
    payload = json.loads(result.content[0].text)
    assert payload["errors"], "a missing path must be reported, not silently passed"
