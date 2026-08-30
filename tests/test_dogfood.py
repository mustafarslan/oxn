"""The self-repair harness, tested without calling a model.

The loop drives two models, but almost all of it is plumbing: locating a function, splicing
a replacement by byte range, deciding whether a candidate is a genuine simplification or a
shredding. That plumbing is where the bugs live, so it is tested deterministically and the
`llm` lane covers one live end-to-end run.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_harness():
    """Import `scripts/dogfood.py`, which is dev tooling rather than a package module."""
    spec = importlib.util.spec_from_file_location("dogfood", ROOT / "scripts" / "dogfood.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["dogfood"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def harness():
    return load_harness()


# ---- locating and splicing ------------------------------------------------------------------


def test_function_span_covers_decorators(harness, tmp_path) -> None:
    """A replacement must include decorators, or applying it duplicates them."""
    from oxn.profiles import get_profile

    source = tmp_path / "m.py"
    source.write_text("import functools\n\n\n@functools.cache\ndef target(a):\n    return a\n")
    span = harness.function_span(source, "target", get_profile("python"))
    assert span is not None
    text = source.read_bytes()[span[0] : span[1]].decode()
    assert text.startswith("@functools.cache")
    assert "def target" in text


def test_splicing_by_byte_range_replaces_exactly_the_function(harness, tmp_path) -> None:
    """Byte-range splicing rather than applying a model-written diff, which mis-applies."""
    from oxn.profiles import get_profile

    source = tmp_path / "m.py"
    original = "HEADER = 1\n\n\ndef target(a):\n    return a\n\n\nFOOTER = 2\n"
    source.write_text(original)

    span = harness.function_span(source, "target", get_profile("python"))
    data = source.read_bytes()
    replacement = b"def target(a):\n    return a * 2\n"
    patched = (data[: span[0]] + replacement + data[span[1] :]).decode()

    assert "HEADER = 1" in patched
    assert "FOOTER = 2" in patched
    assert "return a * 2" in patched
    assert patched.count("def target") == 1


def test_a_missing_function_is_reported_not_guessed(harness, tmp_path) -> None:
    from oxn.profiles import get_profile

    source = tmp_path / "m.py"
    source.write_text("def other():\n    pass\n")
    assert harness.function_span(source, "absent", get_profile("python")) is None


# ---- the actor's reply ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "def f(a):\n    return a\n",
        "```python\ndef f(a):\n    return a\n```",
        "Here is the refactor:\n\n```python\ndef f(a):\n    return a\n```\n\nHope that helps!",
        "```\ndef f(a):\n    return a\n```",
    ],
)
def test_fences_and_prose_are_stripped(harness, reply: str) -> None:
    """Models add fences and commentary however firmly the prompt forbids it."""
    cleaned = harness._strip_fences(reply)
    assert cleaned.startswith("def f(a):")
    assert "```" not in cleaned
    assert "Hope that helps" not in cleaned


# ---- the shredding detector --------------------------------------------------------------------


def gauntlet(harness, **kwargs):
    defaults = {
        "tests_pass": True,
        "lint_pass": True,
        "types_pass": True,
        "score_before": 30.0,
        "score_after": 10.0,
        "file_mass_before": 100.0,
        "file_mass_after": 100.0,
        "functions_before": 5,
        "functions_after": 5,
        "ceiling": 12.0,
        "target_present": True,
    }
    return harness.GauntletResult(**{**defaults, **kwargs})


def test_a_genuine_simplification_passes(harness) -> None:
    result = gauntlet(harness, file_mass_after=78.0)
    assert result.improved
    assert not result.shredded
    assert result.passed


def test_shredding_is_rejected_even_though_the_score_fell(harness) -> None:
    """The failure docs/metrics.md 10.5 predicts: complexity moved, not removed.

    One function's score drops because it was scattered across new helpers, while the
    file's total complexity mass barely moves. A score-only gate would accept this.
    """
    result = gauntlet(harness, functions_after=12, file_mass_after=99.0)
    assert result.improved, "the target's own score did fall"
    assert result.shredded
    assert not result.passed


def test_extracting_a_few_helpers_that_genuinely_reduce_mass_is_allowed(harness) -> None:
    result = gauntlet(harness, functions_after=9, file_mass_after=70.0)
    assert not result.shredded
    assert result.passed


def test_a_failing_test_rejects_regardless_of_the_score(harness) -> None:
    assert not gauntlet(harness, tests_pass=False, file_mass_after=40.0).passed


def test_lint_and_type_failures_reject(harness) -> None:
    assert not gauntlet(harness, lint_pass=False).passed
    assert not gauntlet(harness, types_pass=False).passed


def test_no_improvement_is_not_an_acceptance(harness) -> None:
    assert not gauntlet(harness, score_after=30.0).passed


def test_deleting_the_function_is_not_repairing_it(harness) -> None:
    """A live run produced exactly this, and the gauntlet nearly rewarded it.

    Asked to simplify `_imported_names`, the actor removed it. The measurement then found
    no row for the target and reported a score of zero -- the lowest possible, so
    `improved` was true and the deletion read as the strongest refactoring in the log. Only
    the test suite caught it, and only because that particular function had a caller under
    test; an uncovered one would have been accepted outright.
    """
    result = gauntlet(harness, target_present=False, score_after=0.0, functions_after=4)
    assert result.improved, "the score did fall -- that is precisely the trap"
    assert not result.passed


def test_getting_closer_to_the_ceiling_is_not_reaching_it(harness) -> None:
    """Also from a live run: 35 -> 14 against a ceiling of 12.

    Real work, and a real improvement, but OXN would still block the result. A harness that
    accepts what the tool rejects is measuring something other than the tool.
    """
    result = gauntlet(harness, score_before=35.0, score_after=14.0, file_mass_after=80.0)
    assert result.improved
    assert not result.under_ceiling
    assert not result.passed


def test_landing_exactly_on_the_ceiling_is_an_acceptance(harness) -> None:
    """The ceiling is a maximum, not a bound to beat."""
    assert gauntlet(harness, score_after=12.0, file_mass_after=80.0).passed


# ---- the dry-run client -------------------------------------------------------------------------


def test_the_fake_client_makes_the_loop_testable(harness) -> None:
    """A stand-in that duck-types the real client, so plumbing tests need no model."""
    client = harness._FakeClient()
    assert "def " in client.generate("anything")
    assert client.generate_json("anything")["verdict"] == "reject"
