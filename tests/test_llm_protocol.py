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


def test_a_stream_that_never_carries_a_response_field_is_an_error(patched: Any) -> None:
    """Distinct from an empty `response`: here the field never appears at all."""
    patched([{"thinking": "hmm", "done": True}])
    with pytest.raises(OllamaError, match="empty completion"):
        _client().generate("prompt")


def test_an_empty_completion_is_an_error_not_an_empty_string(patched: Any) -> None:
    """A live run produced exactly this: 67 seconds of thinking, then nothing.

    Returning "" pushes the failure downstream, where it wears the caller's face -- the
    repair harness read the empty reply as the model having deleted the function it was
    asked to simplify, and logged it as a model failure.
    """
    patched([{"thinking": "considering the options"}, {"response": "", "done": True}])
    with pytest.raises(OllamaError, match="empty completion"):
        _client().generate("prompt")


def test_whitespace_only_is_equally_empty(patched: Any) -> None:
    patched([{"response": "  \n\n  "}, {"response": "", "done": True}])
    with pytest.raises(OllamaError, match="empty completion"):
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


# ---- an empty answer must say why -------------------------------------------------------


def test_reasoning_that_hits_the_token_limit_says_so(patched: Any) -> None:
    """The failure that took three live attempts to understand.

    `glm-5.3:cloud` streamed 11,928 frames on a repair prompt and never emitted a single
    character of `response`. The old message -- "empty completion after 11928 chunk(s)" --
    could not distinguish that from a crash, a bad model name or a refusal, so the run had
    to be reproduced by hand to learn anything. The three cases need different fixes, so
    the message names which one happened.
    """
    patched(
        [
            {"thinking": "x" * 4000, "response": ""},
            {"thinking": "y" * 4000, "response": ""},
            {"response": "", "done": True, "done_reason": "length"},
        ]
    )
    with pytest.raises(OllamaError, match="token limit while reasoning"):
        _client().generate("prompt")


def test_the_message_counts_the_reasoning_it_threw_away(patched: Any) -> None:
    patched([{"thinking": "z" * 1234, "response": ""}, {"response": "", "done": True}])
    with pytest.raises(OllamaError, match="1,234 characters of reasoning"):
        _client().generate("prompt")


def test_nothing_at_all_is_reported_differently_from_reasoning(patched: Any) -> None:
    """No reasoning and no text is a different problem from reasoning without an answer."""
    patched([{"response": "", "done": True, "done_reason": "stop"}])
    with pytest.raises(OllamaError, match="no reasoning and no text"):
        _client().generate("prompt")


def test_reasoning_chunks_do_not_count_as_an_answer(patched: Any) -> None:
    """`thinking` resets the read clock; it is never spliced into a repair."""
    patched([{"thinking": "long deliberation", "response": ""}, {"response": "OK", "done": True}])
    assert _client().generate("prompt") == "OK"


def test_the_token_budget_is_actually_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The knobs the failure message names must be ones OXN sets.

    Before this, `generate` sent only `temperature`, so a repair ran on Ollama's small
    client-side context default however large a window the model advertised -- and the
    error told the operator to raise `num_ctx`, which OXN neither sent nor exposed.
    """
    captured: dict[str, Any] = {}

    class _Frames:
        def __enter__(self) -> Any:
            return iter([b'{"response": "OK", "done": true}'])

        def __exit__(self, *exc: object) -> None:
            return None

    def fake_urlopen(request: Any, timeout: float = 0) -> Any:  # noqa: ARG001
        captured.update(json.loads(request.data))
        return _Frames()

    monkeypatch.setattr("oxn.llm.urllib.request.urlopen", fake_urlopen)
    OllamaClient(num_ctx=99, num_predict=77).generate("prompt")

    assert captured["options"]["num_ctx"] == 99
    assert captured["options"]["num_predict"] == 77


def test_the_budget_is_configurable_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OXN_OLLAMA_NUM_CTX", "4096")
    monkeypatch.setenv("OXN_OLLAMA_NUM_PREDICT", "256")
    client = OllamaClient.from_env()
    assert (client.num_ctx, client.num_predict) == (4096, 256)


# ---- a model that reasons itself out of budget ------------------------------------------


def _capturing(monkeypatch: pytest.MonkeyPatch, *responses: list[dict[str, Any]]) -> list[Any]:
    """Serve one framed response per call, recording each request payload."""
    sent: list[Any] = []
    pending = list(responses)

    class _Frames:
        def __init__(self, frames: list[dict[str, Any]]) -> None:
            self.frames = frames

        def __enter__(self) -> Any:
            return iter([json.dumps(f).encode() for f in self.frames])

        def __exit__(self, *exc: object) -> None:
            return None

    def fake_urlopen(request: Any, timeout: float = 0) -> Any:  # noqa: ARG001
        sent.append(json.loads(request.data))
        return _Frames(pending.pop(0))

    monkeypatch.setattr("oxn.llm.urllib.request.urlopen", fake_urlopen)
    return sent


STARVED = [
    {"thinking": "x" * 500, "response": ""},
    {"response": "", "done": True, "done_reason": "length"},
]
ANSWERED = [{"response": "def f(): ...", "done": True, "done_reason": "stop"}]


def test_a_model_that_reasons_out_of_budget_is_retried_without_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured on `glm-5.3:cloud`: raising the budget does not fix this.

    Asked to simplify `build_file` it spent its whole allowance reasoning and answered
    nothing -- ~8k tokens against a budget of 8192, then ~32.6k against 32768. It scales
    deliberation to whatever room it is given, so a bigger number buys a slower failure.
    Turning thinking off returned code on the same prompt.
    """
    sent = _capturing(monkeypatch, STARVED, ANSWERED)
    assert OllamaClient().generate("prompt") == "def f(): ..."

    assert len(sent) == 2, "the starved attempt must be retried"
    assert "think" not in sent[0], "the first attempt takes the model's default"
    assert sent[1]["think"] is False


def test_the_retry_happens_once_and_the_error_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    """If it starves without thinking too, that is the answer -- not a third attempt."""
    sent = _capturing(monkeypatch, STARVED, STARVED)
    with pytest.raises(OllamaError, match="token limit while reasoning"):
        OllamaClient().generate("prompt")
    assert len(sent) == 2


def test_a_caller_who_asked_for_reasoning_is_not_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit preference is respected: the error beats silently different behaviour."""
    sent = _capturing(monkeypatch, STARVED)
    with pytest.raises(OllamaError, match="token limit while reasoning"):
        OllamaClient(think=True).generate("prompt")
    assert len(sent) == 1
    assert sent[0]["think"] is True


def test_an_ordinary_empty_completion_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only starvation is worth retrying. A clean stop with no text is a different problem."""
    sent = _capturing(monkeypatch, [{"response": "", "done": True, "done_reason": "stop"}])
    with pytest.raises(OllamaError, match="no reasoning and no text"):
        OllamaClient().generate("prompt")
    assert len(sent) == 1


def test_an_answer_cut_off_at_the_token_limit_is_an_error_not_an_answer(patched: Any) -> None:
    """`stopped_because` was captured and read only when the completion was *empty*.

    A non-empty one carrying the same `done_reason` is the same fact with text attached, and
    it came back as though it were finished. Measured on the httpx bed: `glm-5.3:cloud` wrote
    123,471 characters, hit `num_predict`, and the harness scored the half-written function
    as `tests FAIL types FAIL target_present False` -- indistinguishable from a bad repair.

    The message names the budget, because raising it is the fix and the number is per-client.
    """
    patched(
        [
            {"thinking": "weighing the options"},
            {"response": "def f():\n    if x:\n        return"},
            {"response": "", "done": True, "done_reason": "length"},
        ]
    )
    with pytest.raises(OllamaError, match="stopped at the token limit") as raised:
        _client().generate("prompt")

    assert "num_predict" in str(raised.value)
    assert "characters of answer" in str(raised.value)


def test_a_complete_answer_that_happens_to_end_at_done_is_returned(patched: Any) -> None:
    """The control. `done_reason=stop` is a model that finished, however long it took."""
    patched(
        [{"response": "def f(): return 1"}, {"response": "", "done": True, "done_reason": "stop"}]
    )
    assert _client().generate("prompt") == "def f(): return 1"
