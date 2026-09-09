"""What may drive the repair harness, and how it is chosen.

P11 states the constraint the harness had already broken: *"Nothing in the harness should
assume Ollama; the current shape must not calcify."* It did. `Session`'s docstring said "the
loop wants *an* actor and *a* judge, and which ones is the operator's business" while
`_clients` named `OllamaClient` outright and `--host`, an Ollama concept, sat on the session
as though it were general.

The arm that decides what the evaluation *means* is not a local model at all: it is Claude
Code driven through OXN's own hooks and MCP server, because that is the agent the tool exists
to govern. A result measured only against a 7B model answers a question nobody asked.

These tests spend no tokens. The Claude Code backend takes an injectable runner, so what is
checked is the thing that can be wrong without anyone noticing -- the command, the flags, and
what happens to a reply that is not the JSON the judge asked for.
"""

from __future__ import annotations

import pytest
from tests.test_dogfood import load_harness


@pytest.fixture(scope="module")
def actors():
    return load_harness()


def test_every_backend_the_help_offers_can_actually_be_built(actors) -> None:
    """`--choices` and the registry are the same list, or `--help` advertises a dead name."""
    assert set(actors.backend_names()) == {"ollama", "claude-code", "dry-run"}
    assert set(actors.backend_names()) == set(actors.JUDGE_REGISTRY)


def test_an_unknown_backend_is_refused_rather_than_defaulted(actors) -> None:
    """A typo that fell back to Ollama would file one arm's numbers under another's name.

    That is the failure that makes an arm table meaningless, so it is a hard error and the
    message lists what exists.
    """
    with pytest.raises(SystemExit) as raised:
        actors.build("ollma")
    assert "ollma" in str(raised.value) and "ollama" in str(raised.value)


def test_the_dry_run_backend_reaches_the_loop_by_the_same_path_as_a_real_one(actors) -> None:
    """`--dry-run` is a backend, not a branch around the backend.

    A stand-in reached by a different route tests a different thing: the point of the dry run
    is that the plumbing a real backend uses is the plumbing that ran.
    """
    built = actors.build("dry-run")
    assert built.model == "dry-run"
    assert "def helper" in built.generate("Reply with the complete replacement for `helper`.")


def test_claude_code_is_driven_in_print_mode_with_the_model_asked_for(actors) -> None:
    """The command, checked without running it."""
    seen: dict[str, object] = {}

    def fake(command, prompt):
        seen["command"] = list(command)
        seen["prompt"] = prompt
        return "def helper():\n    return None\n"

    client = actors.ClaudeCodeClient(model="claude-opus-5", run=fake)
    answer = client.generate("fix this", system="be terse")

    assert seen["command"][:2] == ["claude", "-p"]
    assert "--model" in seen["command"] and "claude-opus-5" in seen["command"]
    assert "--append-system-prompt" in seen["command"] and "be terse" in seen["command"]
    assert seen["prompt"] == "fix this"
    assert answer.startswith("def helper")


def test_a_judge_reply_wrapped_in_prose_is_still_read(actors) -> None:
    """A model that adds a sentence around its JSON has not refused the repair.

    Demanding bare JSON and failing the attempt scores a rejection the judge never made,
    which is a fabricated data point in a table whose whole purpose is counting them.
    """
    client = actors.ClaudeCodeClient(
        run=lambda command, prompt: (
            'Sure! {"verdict": "accept", "genuinely_simpler": true} Hope that helps.'
        )
    )
    assert client.generate_json("judge this") == {
        "verdict": "accept",
        "genuinely_simpler": True,
    }


def test_a_judge_reply_with_no_json_is_a_rejection_that_says_why(actors) -> None:
    """Unparseable is not acceptable, and the reason is recorded rather than guessed at."""
    client = actors.ClaudeCodeClient(run=lambda command, prompt: "I would rather not.")
    verdict = client.generate_json("judge this")
    assert verdict["verdict"] == "reject"
    assert "no JSON" in verdict["concerns"][0]


def test_a_missing_claude_binary_is_an_environment_error_not_an_empty_answer(
    actors, monkeypatch
) -> None:
    """A silent empty reply would enter the log as an actor that declined every repair."""
    monkeypatch.setattr(actors.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit) as raised:
        actors.ClaudeCodeClient().generate("anything")
    assert "claude" in str(raised.value)
