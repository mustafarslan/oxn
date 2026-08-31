"""A minimal Ollama client, for the parts of OXN that genuinely need a model.

**Most of OXN must never call one.** ``idea.md`` §1.1 is the project's founding argument:
computing a metric with a language model gets you an approximation of something a parser
knows exactly. Every metric in this codebase is deterministic, and that is the point.

Inference belongs in exactly three places:

* **semantic ADR retrieval** -- ranking prose by relevance, which is what embeddings are for;
* **the diagnostic compressor** on the research track -- condensing multi-page tool output
  into a token-efficient refactor plan;
* **evaluation** -- driving or judging an agent, where the model is the thing under test.

Configuration is environment-first so a test lane can point at a different host without
touching code. Standard library only: this must never become a runtime dependency of the
metric engine, and ``tests/test_import_guard.py`` enforces that.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable


#: Default endpoint and models. Overridable through the environment.
#:
#: The **actor** writes code and the **judge** assesses it, and they are deliberately
#: different models: a model grading its own output is not an independent check, and the
#: agreement rate between judge and deterministic gauntlet is itself a result worth having.
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "glm-5.3:cloud"
DEFAULT_JUDGE_MODEL = "deepseek-v4-pro:cloud"


class OllamaError(RuntimeError):
    """Raised when Ollama is unreachable or returns something unusable."""


@dataclass(frozen=True, slots=True)
class OllamaClient:
    """A thin wrapper over Ollama's HTTP API."""

    host: str = DEFAULT_HOST
    model: str = DEFAULT_MODEL
    #: Seconds of **silence** tolerated, not total generation time -- see `generate`. A
    #: 428-second repair on `glm-5.3:cloud` streamed 37,869 frames with a largest gap of
    #: 8.0s, so this leaves an order of magnitude of headroom while still noticing a stream
    #: that has genuinely stopped. Raise `OXN_OLLAMA_TIMEOUT` for a slower link.
    timeout: int = 120
    #: Context window, in tokens. Ollama applies a small client-side default (4k on the
    #: versions in use) regardless of what the model supports -- `glm-5.3:cloud` advertises
    #: 1,048,576 -- and a repair prompt plus a reasoning model's scratchpad does not fit in
    #: 4k. When it does not fit, the model reasons until the window is full and returns an
    #: empty completion, which is exactly how three `build_file` repairs failed.
    num_ctx: int = 65536
    #: Cap on generated tokens, **reasoning included** -- which is why it is this large.
    #: Measured: `glm-5.3:cloud` spent 31,864 characters (~8k tokens) reasoning about one
    #: `build_file` repair and was cut off mid-thought at a budget of 8192, twice, with
    #: `done_reason == "length"`. A reasoning actor needs room to think *and then* answer.
    #: Generous rather than unlimited: an unlimited budget turns a model that will not stop
    #: into a hung run, and the read timeout only notices silence, not a model still
    #: happily thinking.
    num_predict: int = 32768
    #: Whether to let a reasoning model think. `None` means "say nothing and take the
    #: model's default", which is right for models that have no such mode. Setting it False
    #: is what the fallback below does after a model reasons itself out of budget.
    think: bool | None = None

    @classmethod
    def from_env(cls) -> OllamaClient:
        """The actor: the model that writes."""
        return cls(
            host=os.environ.get("OXN_OLLAMA_HOST", DEFAULT_HOST),
            model=os.environ.get("OXN_OLLAMA_MODEL", DEFAULT_MODEL),
            timeout=int(os.environ.get("OXN_OLLAMA_TIMEOUT", "120")),
            num_ctx=int(os.environ.get("OXN_OLLAMA_NUM_CTX", "65536")),
            num_predict=int(os.environ.get("OXN_OLLAMA_NUM_PREDICT", "32768")),
        )

    @classmethod
    def judge_from_env(cls) -> OllamaClient:
        """The judge: a *different* model, so an assessment is not a self-assessment."""
        return cls(
            host=os.environ.get("OXN_OLLAMA_HOST", DEFAULT_HOST),
            model=os.environ.get("OXN_OLLAMA_JUDGE_MODEL", DEFAULT_JUDGE_MODEL),
            timeout=int(os.environ.get("OXN_OLLAMA_TIMEOUT", "120")),
            num_ctx=int(os.environ.get("OXN_OLLAMA_NUM_CTX", "65536")),
            num_predict=int(os.environ.get("OXN_OLLAMA_NUM_PREDICT", "32768")),
        )

    def generate_json(self, prompt: str, *, system: str = "") -> dict[str, object]:
        """A completion parsed as JSON, tolerating the fences models like to add.

        Structured output is what makes a judge's verdict usable: "is this good?" gives an
        opinion, while explicit fields give something that can be tabulated and calibrated.
        """
        text = self.generate(prompt, system=system)
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end == -1:
            raise OllamaError(f"expected JSON, got: {text[:200]!r}")
        try:
            parsed = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as error:
            raise OllamaError(f"invalid JSON from {self.model}: {error}") from error
        if not isinstance(parsed, dict):
            raise OllamaError(f"expected a JSON object, got {type(parsed).__name__}")
        return parsed

    def available(self) -> bool:
        """True when the host answers. Used to skip rather than fail a test lane."""
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=5) as response:  # noqa: S310
                return bool(response.status == 200)
        except (urllib.error.URLError, OSError, TimeoutError):
            return False

    def generate(self, prompt: str, *, temperature: float = 0.0, system: str = "") -> str:
        """One completion. Temperature defaults to 0, which is not the same as determinism.

        It removes the sampler as a source of variation and nothing else: three runs of the
        identical repair prompt against `glm-5.3:cloud` at temperature 0 returned three
        materially different rewrites. Batching and routing on a hosted endpoint are not
        under a caller's control, so a test that needs a fixed answer must pin the answer,
        not the temperature.

        The request **streams**, and that is a correctness decision rather than a cosmetic
        one. With ``stream: false`` the server stays silent for the whole generation, so the
        socket timeout degenerates into a total wall-clock limit: a reasoning model that
        thinks for longer than it fails outright, however healthy the connection. Streaming
        makes the same number mean *no progress for this long*, which is the condition worth
        aborting on. A 383-second repair -- measured, on ``glm-5.3:cloud`` -- flipped between
        success and "unreachable" against a 600-second blocking read; under streaming it is
        simply a slow answer.

        Chunks carrying only ``thinking`` still count as progress: they reset the read clock
        by arriving, and contribute nothing to the completion.
        """
        reply = self._attempt(prompt, temperature, system, think=self.think)
        # Measured, and not recoverable by raising the budget: `glm-5.3:cloud` asked to
        # simplify `build_file` spent its whole allowance reasoning and answered nothing --
        # ~8k tokens against a budget of 8192, then ~32.6k against 32768. It scales its
        # deliberation to whatever room it is given, so a bigger number buys another failure
        # more slowly. Turning thinking off is the fix the model itself supports: the same
        # prompt then returns 3,146 characters of code instead of 45.
        #
        # Only when the caller expressed no preference, and only once. A caller that asked
        # for reasoning gets the error instead of silently different behaviour.
        if self.think is None and reply.starved_by_reasoning:
            reply = self._attempt(prompt, temperature, system, think=False)

        if not reply.saw_completion or not reply.text:
            raise OllamaError(f"{self.model} returned an empty completion: {reply.why_empty}")
        return reply.text

    def _attempt(
        self, prompt: str, temperature: float, system: str, *, think: bool | None
    ) -> _Reply:
        """One request/response cycle. Raises for transport problems, never for content."""
        payload: dict[str, object] = {
            "model": self.model,
            "prompt": prompt,
            "stream": True,
            "options": {
                "temperature": temperature,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
            },
        }
        if system:
            payload["system"] = system
        if think is not None:
            payload["think"] = think

        request = urllib.request.Request(  # noqa: S310 - a configured local endpoint
            f"{self.host}/api/generate",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                reply = self._consume(response)
        except TimeoutError as error:
            raise OllamaError(
                f"{self.model} sent nothing for {self.timeout}s -- it may still be "
                f"generating; raise OXN_OLLAMA_TIMEOUT if so ({error})"
            ) from error
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            if isinstance(reason, TimeoutError):
                raise OllamaError(
                    f"Ollama at {self.host} did not answer within {self.timeout}s"
                ) from error
            raise OllamaError(f"Ollama at {self.host} is unreachable: {error}") from error
        except json.JSONDecodeError as error:
            raise OllamaError(f"Ollama returned invalid JSON: {error}") from error
        return reply

    def _consume(self, response: Iterable[bytes]) -> _Reply:
        """Fold the streamed frames into one reply.

        Chunks carrying only ``thinking`` still count as progress: they reset the read
        clock by arriving, and contribute nothing to the completion.
        """
        reply = _Reply()
        for line in response:
            stripped = line.strip()
            if not stripped:
                continue
            frame = json.loads(stripped.decode())
            if error_text := frame.get("error"):
                raise OllamaError(f"{self.model} reported: {error_text}")
            reply.take(frame)
            if frame.get("done"):
                reply.stopped_because = str(frame.get("done_reason") or "")
                break
        return reply


@dataclass
class _Reply:
    """What one streamed generation produced, including what it produced *instead* of text.

    The reasoning is kept for the failure message and never for the answer. A model that
    never leaves its scratchpad emits thousands of frames and an empty completion, and
    "empty completion" alone cannot say whether it crashed, was cut off, or simply thought
    until it ran out of room -- three problems with three different fixes.
    """

    chunks: list[str] = field(default_factory=list)
    saw_completion: bool = False
    thinking_chars: int = 0
    frames: int = 0
    stopped_because: str = ""

    def take(self, frame: dict[str, Any]) -> None:
        self.frames += 1
        reasoning = frame.get("thinking")
        if isinstance(reasoning, str):
            self.thinking_chars += len(reasoning)
        piece = frame.get("response")
        if isinstance(piece, str):
            self.saw_completion = True
            self.chunks.append(piece)

    @property
    def text(self) -> str:
        return "".join(self.chunks).strip()

    @property
    def starved_by_reasoning(self) -> bool:
        """Did the model think until it ran out of room, and never answer?

        The one empty completion worth retrying differently: it is not a refusal, a crash
        or a bad prompt, it is a model that never left its scratchpad.
        """
        return bool(self.thinking_chars) and self.stopped_because == "length" and not self.text

    @property
    def why_empty(self) -> str:
        """Say what the model actually did, so an empty answer is diagnosable.

        Measured: `glm-5.3:cloud` produced 11,928 frames of reasoning and no completion on
        a repair prompt, and the old message -- "empty completion after 11928 chunk(s)" --
        said none of that, so the run had to be reproduced by hand to learn anything.
        """
        if not self.thinking_chars:
            detail = f"{self.frames} frame(s), no reasoning and no text"
        else:
            detail = (
                f"{self.thinking_chars:,} characters of reasoning across "
                f"{self.frames} frame(s), then no answer"
            )
        if self.stopped_because == "length":
            return (
                f"{detail}. It hit the token limit while reasoning -- raise `num_predict` "
                f"or `num_ctx`, or use a model that reasons less."
            )
        if self.stopped_because:
            return f"{detail} (done_reason={self.stopped_because})."
        return f"{detail} (the stream ended without a done frame)."
