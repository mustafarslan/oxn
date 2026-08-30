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
from dataclasses import dataclass

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

    @classmethod
    def from_env(cls) -> OllamaClient:
        """The actor: the model that writes."""
        return cls(
            host=os.environ.get("OXN_OLLAMA_HOST", DEFAULT_HOST),
            model=os.environ.get("OXN_OLLAMA_MODEL", DEFAULT_MODEL),
            timeout=int(os.environ.get("OXN_OLLAMA_TIMEOUT", "120")),
        )

    @classmethod
    def judge_from_env(cls) -> OllamaClient:
        """The judge: a *different* model, so an assessment is not a self-assessment."""
        return cls(
            host=os.environ.get("OXN_OLLAMA_HOST", DEFAULT_HOST),
            model=os.environ.get("OXN_OLLAMA_JUDGE_MODEL", DEFAULT_JUDGE_MODEL),
            timeout=int(os.environ.get("OXN_OLLAMA_TIMEOUT", "120")),
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
        payload: dict[str, object] = {
            "model": self.model,
            "prompt": prompt,
            "stream": True,
            "options": {"temperature": temperature},
        }
        if system:
            payload["system"] = system

        request = urllib.request.Request(  # noqa: S310 - a configured local endpoint
            f"{self.host}/api/generate",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        chunks: list[str] = []
        saw_completion = False
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for line in response:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    frame = json.loads(stripped.decode())
                    if error_text := frame.get("error"):
                        raise OllamaError(f"{self.model} reported: {error_text}")
                    piece = frame.get("response")
                    if isinstance(piece, str):
                        saw_completion = True
                        chunks.append(piece)
                    if frame.get("done"):
                        break
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

        if not saw_completion:
            raise OllamaError(f"Ollama returned no completion for {self.model}")
        return "".join(chunks).strip()
