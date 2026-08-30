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

#: Default endpoint and model. Overridable via ``OXN_OLLAMA_HOST`` / ``OXN_OLLAMA_MODEL``.
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "glm-5.3:cloud"


class OllamaError(RuntimeError):
    """Raised when Ollama is unreachable or returns something unusable."""


@dataclass(frozen=True, slots=True)
class OllamaClient:
    """A thin wrapper over Ollama's HTTP API."""

    host: str = DEFAULT_HOST
    model: str = DEFAULT_MODEL
    timeout: int = 180

    @classmethod
    def from_env(cls) -> OllamaClient:
        return cls(
            host=os.environ.get("OXN_OLLAMA_HOST", DEFAULT_HOST),
            model=os.environ.get("OXN_OLLAMA_MODEL", DEFAULT_MODEL),
            timeout=int(os.environ.get("OXN_OLLAMA_TIMEOUT", "180")),
        )

    def available(self) -> bool:
        """True when the host answers. Used to skip rather than fail a test lane."""
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=5) as response:  # noqa: S310
                return bool(response.status == 200)
        except (urllib.error.URLError, OSError, TimeoutError):
            return False

    def generate(self, prompt: str, *, temperature: float = 0.0, system: str = "") -> str:
        """One completion. Temperature defaults to 0 so a test is reproducible."""
        payload: dict[str, object] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if system:
            payload["system"] = system

        request = urllib.request.Request(  # noqa: S310 - a configured local endpoint
            f"{self.host}/api/generate",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode())
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise OllamaError(f"Ollama at {self.host} is unreachable: {error}") from error
        except json.JSONDecodeError as error:
            raise OllamaError(f"Ollama returned invalid JSON: {error}") from error

        text = body.get("response")
        if not isinstance(text, str):
            raise OllamaError(f"Ollama returned no completion: {body!r}")
        return text.strip()
