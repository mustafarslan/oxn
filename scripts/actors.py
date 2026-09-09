"""Actor backends for the repair harness: what drives the loop, chosen at run time.

`scripts/dogfood.py` is the harness P11 turns into the evaluation harness, and P11 states the
constraint plainly: *"Nothing in the harness should assume Ollama; the current shape must not
calcify."* It did assume Ollama. `Session`'s own docstring said "the loop wants *an* actor and
*a* judge, and which ones is the operator's business" while `_clients` named `OllamaClient`
outright, and `--host` -- an Ollama concept -- sat on the session as though it were general.

The arm that actually matters is not a local model at all. It is **Claude Code driven through
OXN's own hooks and MCP server**, because that is the agent the tool exists to govern; a
result measured only against a 7B model answers a question nobody asked. So the backend is a
name, the names are registered here, and adding one is a function rather than an edit to the
loop.

What a backend must do is small on purpose -- `generate` and `generate_json` -- because that
is the whole of what the loop asks. Anything richer would be this module deciding how a
future actor works.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable, Sequence


class Actor(Protocol):
    """What the repair loop needs from whatever is answering it.

    `model` and `host` are for the log rather than the loop: every attempt records which
    actor produced it, and a run whose backend is not identifiable is not a measurement.
    """

    model: str
    host: str

    def generate(self, prompt: str, *, temperature: float = 0.0, system: str = "") -> str: ...

    def generate_json(self, prompt: str, *, system: str = "") -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class Backend:
    """One way of obtaining an actor, and what it is called on the command line."""

    name: str
    describe: str
    build: Callable[[str, str], Actor]


def build(name: str, model: str = "", host: str = "") -> Actor:
    """The actor registered under `name`, configured for this run.

    An unknown name is refused with the list rather than defaulted: a typo that silently
    fell back to Ollama would attribute one backend's numbers to another, and the whole
    point of the arm table is that the arms are distinguishable.
    """
    found = _REGISTRY.get(name)
    if found is None:
        raise SystemExit(
            f"unknown actor backend {name!r}. Available: {', '.join(sorted(_REGISTRY))}"
        )
    return found.build(model, host)


def backend_names() -> tuple[str, ...]:
    """Every registered backend, for `--help` and for the arm table."""
    return tuple(sorted(_REGISTRY))


# ---- the backends -------------------------------------------------------------------------


def _ollama(model: str, host: str) -> Actor:
    """A local model over Ollama's HTTP API -- the harness's original and still its default."""
    import dataclasses

    from oxn.llm import OllamaClient

    changes: dict[str, Any] = {}
    if model:
        changes["model"] = model
    if host:
        changes["host"] = host
    base = OllamaClient.from_env()
    return dataclasses.replace(base, **changes) if changes else base


def _ollama_judge(model: str, host: str) -> Actor:
    """The same, from the judge's own environment: a judge may be a stronger model."""
    import dataclasses

    from oxn.llm import OllamaClient

    changes: dict[str, Any] = {}
    if model:
        changes["model"] = model
    if host:
        changes["host"] = host
    base = OllamaClient.judge_from_env()
    return dataclasses.replace(base, **changes) if changes else base


def _claude_code(model: str, host: str) -> Actor:
    """Claude Code in print mode, which is the arm P11 says the result depends on."""
    del host
    return ClaudeCodeClient(model=model or os.environ.get("OXN_CLAUDE_MODEL", ""))


def _dry_run(model: str, host: str) -> Actor:
    """A deterministic stand-in, so the harness itself has tests that need no model."""
    del model, host
    return FakeClient()


@dataclass(frozen=True, slots=True)
class ClaudeCodeClient:
    """Drives `claude -p`, so the agent under study is the one the tool governs.

    Print mode is the whole interface: one prompt in, one answer out, no session state. That
    is exactly the shape the repair loop already has, and it is why this needs no protocol of
    its own -- the loop cannot tell it apart from a local model.

    The binary is looked up rather than assumed, and its absence is reported as an
    environment problem rather than surfacing as a mysterious empty answer. A run that
    silently produced nothing would enter the log as an actor that declined every repair.
    """

    model: str = ""
    host: str = "(cli)"
    #: Injectable so the command can be tested without spending a token on it.
    run: Callable[[Sequence[str], str], str] | None = None

    def generate(self, prompt: str, *, temperature: float = 0.0, system: str = "") -> str:
        del temperature  # `claude -p` has no temperature flag; the default is the arm
        return self._invoke(prompt, system)

    def generate_json(self, prompt: str, *, system: str = "") -> dict[str, object]:
        """The judge's verdict, read out of whatever prose the model wrapped it in.

        Asking for JSON and parsing the first object is more robust than demanding bare JSON
        and failing the attempt when a model adds a sentence -- and a judge that fails to
        parse is scored as a rejection it never made.
        """
        answer = self._invoke(prompt, system or "Reply with a single JSON object and nothing else.")
        match = re.search(r"\{.*\}", answer, re.DOTALL)
        if match is None:
            return {
                "verdict": "reject",
                "concerns": ["no JSON in reply"],
                "genuinely_simpler": False,
            }
        try:
            found = json.loads(match.group(0))
        except json.JSONDecodeError as error:
            return {"verdict": "reject", "concerns": [f"unparseable JSON: {error}"]}
        return (
            found
            if isinstance(found, dict)
            else {"verdict": "reject", "concerns": ["not an object"]}
        )

    def _invoke(self, prompt: str, system: str) -> str:
        command = ["claude", "-p"]
        if self.model:
            command += ["--model", self.model]
        if system:
            command += ["--append-system-prompt", system]
        if self.run is not None:
            return self.run(command, prompt)
        if shutil.which("claude") is None:
            raise SystemExit(
                "the `claude-code` backend needs the `claude` CLI on PATH. Install Claude Code,"
                " or run with `--backend ollama`."
            )
        finished = subprocess.run(  # noqa: S603
            command, input=prompt, capture_output=True, text=True, check=False
        )
        if finished.returncode != 0:
            raise SystemExit(f"claude -p exited {finished.returncode}: {finished.stderr.strip()}")
        return finished.stdout


class FakeClient:
    """A deterministic stand-in, so the harness itself has tests that need no model."""

    model = "dry-run"
    host = "(none)"

    def generate(self, prompt: str, *, temperature: float = 0.0, system: str = "") -> str:
        """A reply that defines the function actually asked for.

        A fixed `def placeholder()` stopped exercising anything once the harness began
        rejecting candidates that omit the target: every dry run failed at extraction and
        the gauntlet was never reached. Reading the name back out of the prompt keeps the
        dry run a test of the plumbing rather than of the check that guards it.
        """
        del temperature, system
        match = re.search(r"replacement for `([^`]+)`", prompt)
        name = match.group(1) if match else "placeholder"
        return f"def {name}(*args, **kwargs):\n    return None\n"

    def generate_json(self, prompt: str, *, system: str = "") -> dict[str, object]:
        del prompt, system
        return {"verdict": "reject", "concerns": ["dry run"], "genuinely_simpler": False}


#: Every backend the harness can drive. `judge` variants exist because a run may want a
#: stronger model grading a weaker one, which is one of P11's stated factors.
_REGISTRY: dict[str, Backend] = {
    "ollama": Backend("ollama", "a local model over Ollama's HTTP API", _ollama),
    "claude-code": Backend("claude-code", "Claude Code in print mode (`claude -p`)", _claude_code),
    "dry-run": Backend("dry-run", "a deterministic fake; no model is contacted", _dry_run),
}

#: The judge is chosen the same way, and defaults to its own Ollama environment.
JUDGE_REGISTRY: dict[str, Backend] = {
    "ollama": Backend("ollama", "a local model over Ollama's HTTP API", _ollama_judge),
    "claude-code": Backend("claude-code", "Claude Code in print mode (`claude -p`)", _claude_code),
    "dry-run": Backend("dry-run", "a deterministic fake; no model is contacted", _dry_run),
}


def build_judge(name: str, model: str = "", host: str = "") -> Actor:
    """The judge registered under `name`. Same rule for an unknown one as `build`."""
    found = JUDGE_REGISTRY.get(name)
    if found is None:
        raise SystemExit(
            f"unknown judge backend {name!r}. Available: {', '.join(sorted(JUDGE_REGISTRY))}"
        )
    return found.build(model, host)
