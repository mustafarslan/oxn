"""The MCP stdio server: the protocol, and the two properties that break clients.

The protocol here is hand-written (ADR-0001, amended 2026-09-05), so conformance is a claim
this project owes evidence for rather than one it inherits from an SDK. These tests are the
cheap half of that evidence and `tests/test_oracle_mcp.py` is the other half: the official
client, driving this server as a subprocess.

Two failures are worth more than the rest, because both look like a working server right up
until a client is attached:

* **answering a notification** -- traffic on an `id` the client never issued;
* **anything on stdout that is not the wire** -- a `rich` console, a lazily fetched grammar,
  a stray `print`; the client reports a parse error and the actual bug is invisible.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from oxn.server import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    CheckRequest,
    Session,
    Tool,
    serve,
)

ROOT = Path(__file__).resolve().parent.parent


def _request(identifier: int, method: str, **params: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}


def _exchange(*messages: object) -> list[dict]:
    """Drive a session the way the transport does, and collect what it wrote."""
    written: list[str] = []

    class _Sink:
        def write(self, text: str) -> None:
            written.append(text)

        def flush(self) -> None:
            return None

    serve((json.dumps(message) + "\n" for message in messages), out=_Sink())
    return [json.loads(line) for line in "".join(written).splitlines() if line.strip()]


def _call(name: str, **arguments: object) -> dict:
    responses = _exchange(_request(1, "tools/call", name=name, arguments=arguments))
    return responses[0]


# ---- the two that break clients ---------------------------------------------------------


def test_a_notification_is_never_answered() -> None:
    """`notifications/initialized` arrives immediately after the handshake, and a response
    to it is traffic on an id the client never issued.

    Only this test can catch it. The SDK oracle stays green with the guard removed -- its
    client drops a response whose id it never issued -- so the rule holds here at the frame
    level or it holds nowhere. A tolerant client is not permission to emit the frame: the
    next client need not be tolerant, and this is the cheap half of the evidence for a
    protocol OXN implements itself.
    """
    responses = _exchange(
        _request(1, "initialize"),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
        _request(2, "ping"),
    )
    assert [response["id"] for response in responses] == [1, 2]


def test_stdout_carries_nothing_but_the_wire_even_when_a_tool_prints() -> None:
    """A tool that prints must not corrupt the stream.

    Run in a subprocess against a deliberately noisy tool, because the redirect `serve`
    performs is process-wide state that a test cannot honestly fake in-process. Without the
    redirect this fails: the `print` lands between two JSON-RPC frames.
    """
    probe = (
        "import sys, oxn.server as s\n"
        "def noisy(request):\n"
        "    print('a grammar was fetched')\n"
        "    return {'ok': True}\n"
        "tool = s.Tool('noisy', 'prints', s.CheckRequest, noisy)\n"
        "s.serve(session=s.Session([tool]))\n"
    )
    call = json.dumps(_request(1, "tools/call", name="noisy", arguments={}))
    result = subprocess.run(
        [sys.executable, "-c", probe],
        input=call + "\n",
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    for line in result.stdout.splitlines():
        json.loads(line)  # every line on stdout is a frame, or this raises
    assert "a grammar was fetched" in result.stderr


# ---- the handshake ----------------------------------------------------------------------


def test_initialize_echoes_a_version_it_knows() -> None:
    result = _exchange(_request(1, "initialize", protocolVersion="2024-11-05"))[0]["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "oxn"


def test_initialize_substitutes_its_own_version_for_one_it_does_not_know() -> None:
    """Refusing outright over a field neither side uses yet is the worse failure."""
    result = _exchange(_request(1, "initialize", protocolVersion="1999-01-01"))[0]["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION


def test_every_tool_advertises_a_schema_with_its_arguments() -> None:
    tools = _exchange(_request(1, "tools/list"))[0]["result"]["tools"]
    assert {tool["name"] for tool in tools} == {
        "get_architectural_context",
        "check_code",
        "get_metrics",
        "explain_violation",
    }
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["properties"], f"{tool['name']} advertises no arguments"
        assert tool["description"].strip()


# ---- protocol errors and tool errors are different things -------------------------------


def test_an_unknown_method_is_a_protocol_error() -> None:
    response = _exchange(_request(1, "resources/list"))[0]
    assert response["error"]["code"] == METHOD_NOT_FOUND


def test_an_unknown_tool_is_a_protocol_error() -> None:
    assert _call("no_such_tool")["error"]["code"] == INVALID_PARAMS


def test_a_batch_is_refused_rather_than_half_understood() -> None:
    """JSON-RPC batching is not part of this revision of MCP."""
    assert _exchange([_request(1, "ping")])[0]["error"]["code"] == -32600


def test_malformed_json_does_not_end_the_session() -> None:
    written: list[str] = []

    class _Sink:
        def write(self, text: str) -> None:
            written.append(text)

        def flush(self) -> None:
            return None

    serve(iter(["not json\n", json.dumps(_request(2, "ping")) + "\n"]), out=_Sink())
    codes = [json.loads(line).get("error", {}).get("code") for line in written]
    assert codes == [-32700, None]


def test_a_tool_that_raises_is_reported_to_the_agent_not_to_the_plumbing() -> None:
    """A JSON-RPC error reaches the client's transport and shows the model nothing. A
    broken `oxn.yaml` is something the agent can act on, so it comes back as content."""

    def explode(_request: object) -> dict:
        raise RuntimeError("oxn.yaml: unknown key 'ceilingz'")

    session = Session([Tool("boom", "raises", CheckRequest, explode)])
    response = session.respond(_request(1, "tools/call", name="boom", arguments={}))
    assert response is not None
    result = response["result"]
    assert result["isError"] is True
    assert "oxn.yaml: unknown key" in result["content"][0]["text"]


def test_bad_arguments_are_reported_to_the_agent_too() -> None:
    result = _call("check_code", paths="src/oxn/server.py")["result"]
    assert result["isError"] is True
    assert "arguments rejected" in result["content"][0]["text"]


# ---- the two surfaces must not drift ----------------------------------------------------


def test_the_server_and_the_cli_build_the_same_bundle() -> None:
    """`oxn context` and `get_architectural_context` answer the same question, and two
    implementations of "what is this project" is how they start disagreeing about it."""
    task = "raise the cognitive complexity ceiling for the rule engine"
    cli = subprocess.run(
        [sys.executable, "-m", "oxn", "context", task, "-f", "src/oxn/check.py", "--json"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    served = _call("get_architectural_context", task=task, files=["src/oxn/check.py"])
    assert served["result"]["structuredContent"] == json.loads(cli.stdout)


@pytest.mark.parametrize("module", ["mcp", "typer", "rich"])
def test_the_server_does_not_depend_on_the_protocol_sdk(module: str) -> None:
    """ADR-0001's amendment says the SDK is a CI oracle, never a runtime dependency. This
    is what makes that a fact rather than an intention."""
    probe = "import json,sys;import oxn.server;print(json.dumps(sorted(sys.modules)))"
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, cwd=ROOT, check=True
    )
    assert module not in json.loads(out.stdout.splitlines()[-1])


# ---- explaining one finding -------------------------------------------------------------

TANGLED = """def tangled(items, flag, mode):
    total = 0
    for item in items:
        if flag:
            if mode == "a":
                for sub in item:
                    if sub:
                        total += sub
            elif mode == "b":
                total -= 1
    return total
"""

ADR = """---
id: ADR-0001
title: The hot path stays simple
status: accepted
date: 2026-09-05
tags: [complexity]
applies-to: ["src/bad.py"]
constraints:
  ceilings: {cognitive_complexity: 5}
---
# ADR-0001
Because it is read on every request.
"""


@pytest.fixture
def tightened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository where all three ways of declaring a ceiling are in play at once."""
    (tmp_path / "src").mkdir()
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "oxn.yaml").write_text(
        "ceilings:\n  cognitive_complexity: 12\n"
        'layers:\n  hot: ["src/*.py"]\n'
        "layer_ceilings:\n  hot:\n    cognitive_complexity: 8\n"
    )
    (tmp_path / "docs" / "adr" / "0001-tight.md").write_text(ADR)
    (tmp_path / "src" / "bad.py").write_text(TANGLED)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_explain_violation_names_the_declaration_the_gate_actually_used(tightened: Path) -> None:
    """The chain, tightest first: a decision that tightened it, the layer override it
    tightened, and the project default beneath both.

    The equality on the last line is the claim worth having. `Finding` carries a resolved
    number and no provenance, so this join is computed a second way -- from `oxn.yaml` and
    the decision records rather than from the rule engine's `ceiling` relation -- and the
    two must agree. Loosen the ADR to 9 and both the order and the equality change.
    """
    served = _call(
        "explain_violation",
        path="src/bad.py",
        entity="src.bad.tangled",
        rule="cognitive_complexity",
    )["result"]["structuredContent"]

    assert served["status"] == "violation"
    assert [(row["limit"], row["source"]) for row in served["declared_by"]] == [
        (5.0, "docs/adr/0001-tight.md"),
        (8.0, "oxn.yaml"),
        (12.0, "oxn.yaml"),
    ]
    assert served["declared_by"][0]["limit"] == served["finding"]["ceiling"]
    assert "+3 at line 5: `if` nested 2 deep" in served["increments"]


def test_explain_violation_re_measures_rather_than_trusting_its_arguments(
    tightened: Path,
) -> None:
    """Between `check_code` and this call the agent has probably edited the file, and
    "that is fixed" is a better answer than an explanation of something no longer true."""
    (tightened / "src" / "bad.py").write_text("def tangled(items):\n    return sum(items)\n")
    served = _call(
        "explain_violation",
        path="src/bad.py",
        entity="src.bad.tangled",
        rule="cognitive_complexity",
    )["result"]["structuredContent"]

    assert served["status"] == "resolved"
    assert served["finding"] is None
    assert served["declared_by"], "the ceiling still exists; only the violation is gone"


def test_get_metrics_reports_without_judging(tightened: Path) -> None:
    """A ranking, not a verdict: `tangled` is over every ceiling in that tree and this tool
    still has no opinion about it."""
    served = _call("get_metrics", paths=["src"], explain=True)["result"]["structuredContent"]

    assert served["status"] == "OK"
    assert served["entities"][0]["qualified_name"] == "src.bad.tangled"
    assert "violations" not in served, "a report must not carry a verdict"
    assert any("nested 2 deep" in line for line in served["explanation"])


# ---- the warm process, and the one thing it does not refresh ----------------------------


def test_a_failure_from_a_server_older_than_the_code_says_so(monkeypatch) -> None:
    """ADR-0004 makes this process the warm path for the *index*, not for itself.

    Found from the receiving end: `get_architectural_context` answered ``ConfigError:
    'methods_per_class' is not a gated rule`` against an `oxn.yaml` that declares exactly
    that rule, because the server had been running since before the rule existed. Every word
    of the error was true of the code the process holds and false of the code on disk, and
    nothing in it said which -- so the agent's only recourse was to disbelieve the tool.

    On a project that gates itself, a server outliving its own code is the normal case.
    """
    from oxn import server

    def boom(_request):
        raise ValueError("'methods_per_class' is not a gated rule")

    tool = server.Tool(
        name="pretend",
        description="",
        request=server.ContextRequest,
        run=boom,
    )

    monkeypatch.setattr(server, "_SOURCE_STAMP", 1.0)
    monkeypatch.setattr(server, "_newest_source", lambda: 2.0)
    stale = server._ran(tool, server.ContextRequest(task="x"))["content"][0]["text"]

    monkeypatch.setattr(server, "_newest_source", lambda: 1.0)
    current = server._ran(tool, server.ContextRequest(task="x"))["content"][0]["text"]

    assert "is not a gated rule" in current
    assert "Restart the MCP server" not in current, "a current server must not blame itself"
    assert "Restart the MCP server" in stale
