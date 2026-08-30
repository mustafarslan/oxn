"""Wire-level tests for the Ollama client, with no model involved.

`test_llm.py` checks claims *about a model's behaviour* and therefore needs one running.
The protocol is a different matter: framing, timeout semantics and error reporting are
deterministic, so they belong in the default lane where a regression is caught on every
run rather than only when someone has Ollama up.

These tests exist because both bugs they pin were found in production use, not in review:
a 383-second generation was reported as *"Ollama is unreachable"* -- it was not, it was
merely slow -- and the blocking read that produced that message turned the timeout into a
total wall-clock limit, which is not what a timeout should mean.
"""

from __future__ import annotations

import json
import urllib.error
from collections.abc import Iterator
from typing import Any

import pytest

from oxn.llm import OllamaClient, OllamaError


class _Response:
    """Stands in for the file-like object `urlopen` returns, yielding NDJSON frames."""

    def __init__(self, frames: list[dict[str, Any]], *, raise_at: int | None = None) -> None:
        self._frames = frames
        self._raise_at = raise_at

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def __iter__(self) -> Iterator[bytes]:
        for position, frame in enumerate(self._frames):
            if position == self._raise_at:
                raise TimeoutError("timed out")
            yield json.dumps(frame).encode() + b"\n"
        if self._raise_at is not None and self._raise_at >= len(self._frames):
            raise TimeoutError("timed out")


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replaces the transport for one test, returning a factory over the frames to replay."""

    def install(frames: list[dict[str, Any]], **kwargs: Any) -> list[bytes]:
        sent: list[bytes] = []

        def fake_urlopen(request: Any, timeout: float = 0) -> _Response:  # noqa: ARG001
            sent.append(request.data)
            return _Response(frames, **kwargs)

        monkeypatch.setattr("oxn.llm.urllib.request.urlopen", fake_urlopen)
        return sent

    return install


def _client() -> OllamaClient:
    return OllamaClient(model="test-model", timeout=7)


def test_the_request_asks_for_a_stream(patched: Any) -> None:
    """The whole point: without this flag the timeout is a wall-clock limit."""
    sent = patched([{"response": "hi", "done": True}])
    _client().generate("prompt")
    assert json.loads(sent[0])["stream"] is True


def test_chunks_are_assembled_in_order(patched: Any) -> None:
    patched(
        [
            {"response": "def "},
            {"response": "process"},
            {"response": "():"},
            {"response": "", "done": True},
        ]
    )
    assert _client().generate("prompt") == "def process():"


def test_thinking_frames_carry_no_completion(patched: Any) -> None:
    """Reasoning models interleave `thinking`; it is progress, but it is not the answer."""
    patched(
        [
            {"thinking": "the user wants a refactoring"},
            {"thinking": "I should extract the loop body"},
            {"response": "answer", "done": True},
        ]
    )
    assert _client().generate("prompt") == "answer"


def test_reading_stops_at_done(patched: Any) -> None:
    """Anything after `done` is not part of the completion, even if the server sends it."""
    patched([{"response": "kept", "done": True}, {"response": " discarded"}])
    assert _client().generate("prompt") == "kept"


def test_an_error_frame_is_reported_as_itself(patched: Any) -> None:
    patched([{"error": "model 'test-model' not found"}])
    with pytest.raises(OllamaError, match="not found"):
        _client().generate("prompt")


def test_a_stream_with_no_completion_is_an_error(patched: Any) -> None:
    patched([{"thinking": "hmm", "done": True}])
    with pytest.raises(OllamaError, match="no completion"):
        _client().generate("prompt")


def test_a_stall_is_not_reported_as_unreachable(patched: Any) -> None:
    """The bug this file was written for.

    A slow model and a dead host are different problems with different fixes, and the old
    message named the wrong one -- sending a developer to check their network when what
    they needed was a larger `OXN_OLLAMA_TIMEOUT`.
    """
    patched([{"response": "partial"}], raise_at=1)
    with pytest.raises(OllamaError) as caught:
        _client().generate("prompt")
    message = str(caught.value)
    assert "unreachable" not in message
    assert "7s" in message and "OXN_OLLAMA_TIMEOUT" in message


def test_a_dead_host_is_still_reported_as_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: Any, timeout: float = 0) -> None:  # noqa: ARG001
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    monkeypatch.setattr("oxn.llm.urllib.request.urlopen", refuse)
    with pytest.raises(OllamaError, match="unreachable"):
        _client().generate("prompt")


def test_a_connect_timeout_names_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failing to *reach* the host within the budget is distinct from a slow generation."""

    def stall(request: Any, timeout: float = 0) -> None:  # noqa: ARG001
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr("oxn.llm.urllib.request.urlopen", stall)
    with pytest.raises(OllamaError, match="did not answer within 7s"):
        _client().generate("prompt")


def test_blank_lines_between_frames_are_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep-alive newlines are progress and nothing more; they must not break parsing."""

    class _Blanks:
        def __enter__(self) -> _Blanks:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def __iter__(self) -> Iterator[bytes]:
            yield b"\n"
            yield json.dumps({"response": "ok", "done": True}).encode() + b"\n"

    monkeypatch.setattr("oxn.llm.urllib.request.urlopen", lambda *a, **k: _Blanks())  # noqa: ARG005
    assert _client().generate("prompt") == "ok"


def test_json_replies_are_parsed_over_the_stream(patched: Any) -> None:
    """`generate_json` shares the transport, so the judge benefits from the same fix."""
    patched(
        [
            {"response": '```json\n{"behaviour_preserved": true,'},
            {"response": ' "readability": 4}\n```'},
            {"response": "", "done": True},
        ]
    )
    verdict = _client().generate_json("prompt")
    assert verdict == {"behaviour_preserved": True, "readability": 4}
