"""The self-repair harness, tested without calling a model.

The loop drives two models, but almost all of it is plumbing: locating a function, splicing
a replacement by byte range, deciding whether a candidate is a genuine simplification or a
shredding. That plumbing is where the bugs live, so it is tested deterministically and the
`llm` lane covers one live end-to-end run.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent


#: The harness is four modules -- `dogfood` drives, `actor.py` builds the prompts,
#: `actors.py` says what may answer them, and `gauntlet.py` verifies without a model -- and
#: these tests are about the harness rather than about any one of them. The fixture merges
#: them, so a test names what it is testing instead of where the code happens to live this
#: week.
HARNESS_MODULES = (
    "gauntlet",
    "actor",
    "actors",
    "arms",
    "beds",
    "targets",
    "runlog",
    "console",
    "attempt",
    "summary",
    "dogfood",
    "experiment",
)


def load_harness():
    """Import the harness, which is dev tooling rather than a package module.

    `scripts/` goes on the path first: the modules import each other by plain name, and a
    file loaded by location has no package to resolve that against.
    """
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)

    merged = types.SimpleNamespace()
    for name in HARNESS_MODULES:
        spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        for attribute in vars(module):
            if not attribute.startswith("__"):
                setattr(merged, attribute, getattr(module, attribute))
    return merged


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


def test_every_fenced_block_survives_extraction(harness) -> None:
    """A model that answers in two blocks must not lose the second one.

    This is how the harness deleted a function twice. Extraction returned the *first* block
    containing `def `, so a reply that introduced a helper and then gave the rewritten
    target kept the helper and dropped the target -- which was then spliced over the
    target's byte range, leaving its callers with a `NameError`.
    """
    reply = (
        "First a helper:\n\n```python\ndef _alias(node):\n    return node.text\n```\n\n"
        "Then the function itself:\n\n```python\ndef target(node):\n    return _alias(node)\n```"
    )
    cleaned = harness._strip_fences(reply)
    assert "def _alias" in cleaned
    assert "def target" in cleaned


def test_prose_between_blocks_is_never_spliced(harness) -> None:
    """Splitting on the fence marker alternates outside and inside; only inside is code."""
    reply = (
        "```python\ndef a():\n    pass\n```\n\n"
        "and now a second def is mentioned in prose: def not_code\n\n"
        "```python\ndef b():\n    pass\n```"
    )
    cleaned = harness._strip_fences(reply)
    assert "prose" not in cleaned
    assert "not_code" not in cleaned
    assert "def a():" in cleaned and "def b():" in cleaned


@pytest.mark.parametrize(
    ("source", "name", "expected"),
    [
        ("def target(a):\n    return a", "target", True),
        ("async def target(a):\n    return a", "target", True),
        ("    def target(self):\n        return 1", "target", True),
        ("def target_helper(a):\n    return a", "target", False),
        ("def other(a):\n    return a", "target", False),
        ("# def target(a): removed\ndef other(): pass", "target", False),
    ],
)
def test_a_candidate_must_define_the_function_it_replaces(
    harness, source: str, name: str, expected: bool
) -> None:
    """Checked before the gauntlet, because a splice that omits the target deletes it.

    Finding that out from a `NameError` costs a full test, lint and type run first -- five
    minutes to learn something visible in the reply itself.
    """
    assert harness._defines(source, name) is expected


# ---- retry feedback -----------------------------------------------------------------------------


def test_a_rejected_attempt_tells_the_actor_what_went_wrong(harness) -> None:
    """Without this a retry is resampling, not iteration.

    The convergence question the harness exists to ask assumes the actor sees its previous
    failure. At temperature 0 an identical prompt varies only by the model's own
    nondeterminism, which is not a feedback loop.
    """
    feedback = harness._feedback(gauntlet(harness, score_after=14.0, types_pass=False))
    assert "ceiling" in feedback
    assert "14" in feedback and "12" in feedback
    assert "Type checking failed" in feedback


def test_a_deleted_target_is_named_as_the_reason(harness) -> None:
    feedback = harness._feedback(gauntlet(harness, target_present=False, tests_pass=False))
    assert "did not define" in feedback


def test_a_clean_run_that_met_the_ceiling_has_nothing_to_say(harness) -> None:
    assert harness._feedback(gauntlet(harness, score_after=10.0)) == ""


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
        "new_helpers": {},
    }
    return harness.GauntletResult(**{**defaults, **kwargs})


def test_the_gate_is_part_of_the_verdict(harness) -> None:
    """A repair the hook would reject must not be accepted here.

    This is the ruff-format bug's shape, one layer up: the harness ran weaker checks than
    the thing it was standing in for, so it accepted candidates that then failed. Running
    the real gate means any rule added to `oxn check` later is enforced by this harness
    without anyone remembering to come back and add it.
    """
    assert not gauntlet(harness, gate_pass=False).passed
    assert gauntlet(harness, gate_pass=True).passed


def test_the_gate_is_applied_as_a_ratchet_not_a_clean_bill(harness) -> None:
    """The target file legitimately holds debt; the repair must add none.

    A sandbox carries no `.oxn/`, so `--no-baseline` there would fail a repair for
    violations that were in the file before the model touched it -- which is every repair,
    since a file is only chosen because something in it is over a ceiling.
    """
    before = harness.Measurement(
        scores={}, target=30.0, present=True, gate=frozenset({"cognitive_complexity|m.route"})
    )
    after_ok = harness.Measurement(
        scores={}, target=8.0, present=True, gate=frozenset({"cognitive_complexity|m.route"})
    )
    after_shred = harness.Measurement(
        scores={}, target=3.0, present=True, gate=frozenset({"shredding|m.route"})
    )
    assert not (after_ok.gate - before.gate), "pre-existing debt is not the repair's fault"
    assert after_shred.gate - before.gate, "a newly introduced finding is"


def test_the_harness_is_never_looser_than_the_gate(harness) -> None:
    """Two detectors, deliberately not the same one, and the harness is the stricter.

    `oxn check` sees one snapshot, so it can only ask whether a cluster of dedicated
    helpers carries more than the ceiling allows -- which means a shred of a *marginal*
    violation sums to under the ceiling and passes it (complexity mass evaporates under
    extraction; docs/metrics.md section 10.5). The harness has the before-state and needs
    no such reconstruction, so it rejects the same edit.

    The asymmetry is intended. It is a bug only in the other direction, and the shared
    thresholds now live in `oxn.thresholds` so the two cannot drift apart on what a
    trivial helper is.
    """
    from oxn.thresholds import MANY_HELPERS, TRIVIAL_HELPER

    assert harness.TRIVIAL_HELPER == TRIVIAL_HELPER
    assert harness.MANY_HELPERS == MANY_HELPERS

    # A split of an already-legal function: nine trivial helpers, total well under any
    # ceiling. Nothing was evaded, so the gate is right to allow it...
    marginal = gauntlet(harness, new_helpers={f"_h{n}": 1.0 for n in range(9)})
    # ...and the harness still refuses it, because it can see it was one function before.
    assert marginal.shredded


def test_a_genuine_simplification_passes(harness) -> None:
    result = gauntlet(harness, file_mass_after=78.0)
    assert result.improved
    assert not result.shredded
    assert result.passed


#: Measured, not invented. Both are real rewrites of `_imported_names` (35, in
#: `src/oxn/resolve/scopes.py`), scored by OXN after being spliced into the real file.
#:
#: The cohesive one is what `glm-5.3:cloud` produced when extraction was permitted: three
#: helpers named for the three import syntax families. The shred is hand-written to game the
#: same ceiling: sixteen helpers, each a line with a name.
#:
#: They are kept because they are the entire calibration set for `TRIVIAL_HELPER`, and
#: because the previous rule -- mass-based -- got *both* of them backwards. The source of
#: each is preserved beside this file, as `tests/fixtures/cohesive_extraction.py.txt` and
#: `tests/fixtures/shred_control.py.txt`; they are `.txt` because they reference names from
#: `scopes.py` and are provenance rather than importable modules.
COHESIVE_HELPERS = {
    "scopes._ecmascript_imported_names": 4.0,
    "scopes._plain_import_names": 9.0,
    "scopes._from_import_names": 11.0,
}
SHRED_HELPERS = {
    "scopes._is_not_python": 0.0,
    "scopes._is_plain_import": 0.0,
    "scopes._spec": 0.0,
    "scopes._ecma": 1.0,
    "scopes._ecma_specifier": 1.0,
    "scopes._ecma_clause": 1.0,
    "scopes._plain": 1.0,
    "scopes._plain_child": 1.0,
    "scopes._is_alias": 0.0,
    "scopes._plain_alias": 1.0,
    "scopes._plain_dotted": 1.0,
    "scopes._from_import": 1.0,
    "scopes._from_specifier": 1.0,
    "scopes._from_child": 1.0,
    "scopes._from_alias": 1.0,
    "scopes._from_plain": 2.0,
}


def test_a_hand_built_shred_is_rejected(harness) -> None:
    """Sixteen helpers, each a line with a name, and the target down to 2.

    This is the control the rule was validated against, written to game the ceiling on
    purpose. Everything else about it is clean: behaviour preserved, target at 2, every
    helper far under the ceiling.
    """
    result = gauntlet(
        harness,
        score_before=35.0,
        score_after=2.0,
        file_mass_before=137.0,
        file_mass_after=118.0,
        functions_before=19,
        functions_after=35,
        new_helpers=SHRED_HELPERS,
    )
    assert result.improved and result.under_ceiling, "only shredding may reject this"
    assert result.shredded
    assert not result.passed


def test_the_cohesive_extraction_a_model_actually_wrote_is_accepted(harness) -> None:
    """Three helpers named for the three import syntax families, scoring 4, 9 and 11.

    The old mass-based rule rejected exactly this while accepting the shred above, which
    is what the rule change is for.
    """
    result = gauntlet(
        harness,
        score_before=35.0,
        score_after=2.0,
        file_mass_before=137.0,
        file_mass_after=128.0,
        functions_before=19,
        functions_after=22,
        new_helpers=COHESIVE_HELPERS,
    )
    assert not result.shredded
    assert result.passed


def test_mass_no_longer_decides_anything(harness) -> None:
    """The finding that forced the change, stated as a test.

    The shred reduces the file's total *more* than the cohesive extraction does -- 137->118
    against 137->128 -- because every extracted helper restarts at nesting depth zero and
    the nesting increments evaporate. Mass falls monotonically as a function is shredded
    harder, so no threshold on it can separate these two.
    """
    shred = gauntlet(
        harness, file_mass_before=137.0, file_mass_after=118.0, new_helpers=SHRED_HELPERS
    )
    cohesive = gauntlet(
        harness, file_mass_before=137.0, file_mass_after=128.0, new_helpers=COHESIVE_HELPERS
    )
    assert shred.file_mass_after < cohesive.file_mass_after, "the shred looks better by mass"
    assert shred.shredded and not cohesive.shredded, "and substance sees through it"


def test_two_helpers_are_never_a_shred(harness) -> None:
    """Below `MANY_HELPERS` the question is not asked; splitting in two is just splitting."""
    result = gauntlet(harness, new_helpers={"m._a": 0.0, "m._b": 1.0})
    assert not result.shredded


def test_a_helper_over_the_ceiling_is_not_a_repair(harness) -> None:
    """Clearing the ceiling here by writing the next run's violation is not converging."""
    result = gauntlet(harness, score_after=4.0, new_helpers={"m._moved": 24.0})
    assert not result.under_ceiling
    assert not result.passed


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
    client = harness.FakeClient()
    assert "def " in client.generate("anything")
    assert client.generate_json("anything")["verdict"] == "reject"


def test_the_fake_client_defines_the_function_it_was_asked_for(harness) -> None:
    """Otherwise a dry run stops at the define-check and never reaches the gauntlet.

    A fixed `def placeholder()` made every dry run fail at extraction, which tests the
    guard rather than the plumbing the guard protects.
    """
    reply = harness.FakeClient().generate("Reply with the complete replacement for `_walk` only.")
    assert harness._defines(reply, "_walk")


# ---- the extraction arms ------------------------------------------------------------------------


def _prompt(harness, *, allow: bool) -> str:
    """One prompt with every channel open, so only the extraction rule varies.

    Built through `_guidance_block` rather than by formatting the template directly: the
    rules moved into their own block when the arms arrived, and a test that reassembles the
    prompt by hand stops testing the prompt the actor is actually sent.
    """
    ask = harness.Ask(
        target=SimpleNamespace(leaf="f", score=35.0, trail=["x"]),
        ceiling=12,
        file_source="pass",
        allow_extraction=allow,
        arm=harness.arm("hybrid"),
    )
    return str(
        harness.ACTOR_PROMPT.format(
            ceiling=12,
            score=35,
            context=harness._context_block(ask),
            guidance=harness._guidance_block(ask),
            file_source="pass",
            name="f",
            language="python",
            feedback="",
        )
    )


def test_the_two_arms_differ_only_in_whether_extraction_is_allowed(harness) -> None:
    """The experiment the harness was built to run, and it needs both arms intact.

    Banning extraction was the only behaviour until a live run showed what it costs: on a
    dispatch function with a dozen irreducible branches, flattening into comprehensions is
    the sole remaining move, and comprehension clauses cost about what the nesting they
    replace cost. The ceiling is then unreachable by construction, and a failure to
    converge says nothing about the model.
    """
    banned, allowed = _prompt(harness, allow=False), _prompt(harness, allow=True)
    assert "Do NOT split" in banned and "MAY extract" not in banned
    assert "MAY extract" in allowed and "Do NOT split" not in allowed
    assert banned.replace(harness.NO_EXTRACTION, "") == allowed.replace(
        harness.ALLOW_EXTRACTION, ""
    ), "the arms must be identical apart from the rule under test"


def test_permitting_extraction_does_not_disarm_the_shredding_gate(harness) -> None:
    """The gate is what makes the permissive arm safe to run at all."""
    assert "detected and rejected" in harness.ALLOW_EXTRACTION
    assert gauntlet(harness, new_helpers=SHRED_HELPERS).shredded


def test_the_fixture_scores_match_the_preserved_sources(harness) -> None:
    """The scores above are measurements, so the code they were measured from is kept.

    This does not re-measure -- that needs the surrounding `scopes.py` and a subprocess --
    but it does keep the two from drifting apart silently: the helper names in the table
    must be the ones the preserved source actually defines.
    """
    root = Path(__file__).parent / "fixtures"
    for source, expected in (
        ("cohesive_extraction.py.txt", COHESIVE_HELPERS),
        ("shred_control.py.txt", SHRED_HELPERS),
    ):
        text = (root / source).read_text()
        defined = {
            line.split("(")[0].removeprefix("def ").strip()
            for line in text.splitlines()
            if line.startswith("def ")
        }
        named = {name.rsplit(".", 1)[-1] for name in expected}
        assert named <= defined, f"{source} no longer defines {sorted(named - defined)}"
        assert "_imported_names" in defined, f"{source} must still define the target"


# ---- extracting one answer from a reply full of drafts ----------------------------------

#: The shape a reasoning model actually produced: prose, several competing definitions of
#: the target, and a final draft that is syntactically broken. Measured on a real reply --
#: 131,094 characters, 105 fenced blocks, six of them defining `build_file`.
DRAFTING_REPLY = """Let me analyze the problem. The function needs restructuring.

```python
def target(x):
    return x  # first idea
```

Wait, that loses the guard. Let me reconsider.

```python
def target(x):
    if x is None:
        return 0
    return x  # better
```

Hmm, actually I should handle the list case too.

```python
def target(x):
    if x is None:
        return 0
    return [y for y in x  # unterminated
```
"""


def test_a_draft_sequence_yields_the_last_answer_that_parses(harness) -> None:
    """Concatenating every draft produced an unparseable file, six definitions deep.

    The gauntlet then reported that as a failed refactoring rather than as a failure to
    extract one, which is a much more misleading thing to log.
    """
    import ast

    got = harness._strip_fences(DRAFTING_REPLY, "target")
    assert "# better" in got, "the last *parsing* draft is the answer"
    assert "# first idea" not in got, "earlier drafts are not part of it"
    assert "unterminated" not in got, "and neither is the broken final one"
    ast.parse(got)


def test_a_helper_and_the_target_are_both_kept(harness) -> None:
    """The opposite shape, and the reason concatenation exists at all.

    One block holding a helper and the next the rewritten function is a reasonable answer.
    Returning only the first dropped the target and its callers raised `NameError`.
    """
    reply = (
        "Helper first:\n\n```python\ndef _helper(x):\n    return x\n```\n\n"
        "Then the function:\n\n```python\ndef target(x):\n    return _helper(x)\n```\n"
    )
    got = harness._strip_fences(reply, "target")
    assert "_helper" in got and "def target" in got


def test_a_reply_with_no_definition_is_left_for_the_define_check(harness) -> None:
    """Two of the three real replies never defined the target in 133,000 characters.

    Extraction must not invent one; the `_defines` guard rejects these before a full test,
    lint and type run is spent on them.
    """
    reply = "Let me think.\n\n```python\nx = 1\n```\n\nStill thinking.\n"
    assert not harness._defines(harness._strip_fences(reply, "target"), "target")


# ---- labelling the parameter under study -------------------------------------------------


def _working_shred() -> object:
    """A repair that passes everything the bed can check and is a shred by the static rule."""
    from gauntlet import GauntletResult

    return GauntletResult(
        tests_pass=True,
        lint_pass=True,
        types_pass=True,
        score_before=30.0,
        score_after=5.0,
        ceiling=12.0,
        new_helpers={"a": 1.0, "b": 1.0, "c": 2.0, "d": 0.0},
    )


def test_a_shred_that_works_is_still_sent_to_the_judge() -> None:
    """Otherwise the log can only ever confirm the number it was collected under.

    `shredded` is computed from `MANY_HELPERS`, and the judge used to be gated on
    `gauntlet.passed`, which includes `not shredded`. So every candidate with three or more
    trivial helpers was refused by the parameter under study and never labelled, every judged
    row came from below the threshold, and fitting the threshold to them would have recovered
    the threshold. P10 wants fifty labelled extractions; fifty collected that way are worth
    nothing, and the cost of finding out would have been the whole campaign.
    """
    from attempt import _worth_an_opinion

    assert _worth_an_opinion(_working_shred()), "a working shred must still earn an opinion"


def test_the_judge_still_cannot_rescue_a_shred() -> None:
    """The ordering the harness calls its central claim, unchanged by the above.

    Labelling a candidate and accepting it are different acts. `passed` still says no, so
    `one_attempt` cannot accept it however the judge votes -- what changed is that there is
    now an opinion on record next to the refusal, not that the refusal is softer.
    """
    assert not _working_shred().passed


def test_the_logged_result_carries_the_verdicts_and_not_just_the_scores() -> None:
    """All 44 rows written before this recorded `shredded: None` and `passed: None`.

    Both are properties, `asdict` sees fields only, and nothing noticed because the numbers
    beside them looked like a complete record. A log of scores with no verdict cannot answer
    why a candidate was refused, which is the only question a calibration pass asks of it.
    """
    import json

    from attempt import gauntlet_row

    row = gauntlet_row(_working_shred())
    assert row["shredded"] is True
    assert row["passed"] is False
    assert row["helper_count"] == 4
    assert row["helper_median"] == 1.0
    json.dumps(row), "must survive the JSONL writer, which `skipped` as a set would not"


def test_the_go_bed_names_out_the_packages_that_fail_on_their_own_tree() -> None:
    """A bed that cannot verify its pristine tree cannot verify a repair.

    Two go-kit packages fail before anything is repaired, for different reasons, and the
    difference is why neither is handled by widening a floor. `sd/eureka`'s *test binary*
    will not link -- the corpus pins a `golang.org/x/net` whose `internal/socket` still
    references unexported `syscall.recvmsg`, which Go 1.26's linker rejects -- and
    `metrics/cloudwatch`'s `TestGauge` is flaky, three pristine runs giving FAIL, FAIL, ok.

    The flake is the worse one. A deterministic failure blocks the bed and is noticed; a
    coin-flip one is recorded as repairs that sometimes break Go and sometimes do not, which
    is indistinguishable in the log from a real regression. Pinned here because `./...` is
    the natural thing to write and silently readmits both.
    """
    import sys

    sys.path.insert(0, "scripts")
    from beds import bed

    packages = bed("go-kit").verify[0]
    assert not any("eureka" in argument for argument in packages)
    assert "./metrics/cloudwatch/..." not in packages
    assert "./..." not in packages, "`./...` readmits both; the list is the exclusion"
    assert "./metrics/prometheus/..." in packages, "the other nine TestGauge packages stay"


def test_a_bed_that_declares_verification_it_cannot_run_is_still_refused() -> None:
    """"No verification declared" and "declared but unrunnable here" used to be one state.

    typescript-nest's `verify` commands are correct -- `npm test`, `npm run lint` -- and the
    bed still cannot run, because the pinned checkout's dependencies do not install: the
    committed lock file is out of sync with `package.json`, so `npm ci` refuses, and `npm
    install` abandons the pin only to hit a real peer conflict between `@apollo/server` and
    the `graphql` major `@nestjs/apollo` admits. The refusal keyed on empty `verify`, which
    this bed does not have, so it would have been handed 60 targets and failed all of them
    on a missing `vitest`.
    """
    import sys

    import pytest

    sys.path.insert(0, "scripts")
    from beds import bed

    with pytest.raises(SystemExit, match="cannot run it here"):
        bed("typescript-nest")

    for still_fine in ("go-kit", "python-httpx"):
        bed(still_fine)


def test_a_target_is_measurable_with_an_interpreter_that_has_no_oxn(tmp_path) -> None:
    """Every external bed scored `999 -> 999`, and the model was never the reason.

    `measure` ran `{sandbox.python} -m oxn metrics`, using the *bed's* interpreter. That venv
    holds the bed's dependencies and `oxn` is not among them, so the command exited non-zero
    and returned `UNMEASURABLE` -- for `score_before` too, on the untouched file. Nothing
    improves on 999, so `improved` was false for every candidate, none was ever judged, and
    the funnel read as a model that could not refactor. `self` was the only bed where it
    worked, because its venv is this project and therefore has `oxn` in it.

    Measuring is a tree-sitter parse of source text. It never needed the bed's environment,
    and this test pins that by handing it an interpreter path that does not exist.
    """
    import sys
    from types import SimpleNamespace

    sys.path.insert(0, "scripts")
    from gauntlet import UNMEASURABLE, measure
    from targets import Target

    source = tmp_path / "sample.py"
    source.write_text(
        "def knotty(rows, flag):\n"
        "    total = 0\n"
        "    for row in rows:\n"
        "        if flag:\n"
        "            for cell in row:\n"
        "                if cell:\n"
        "                    total += 1\n"
        "                elif cell is None:\n"
        "                    total -= 1\n"
        "    return total\n"
    )
    sandbox = SimpleNamespace(path=tmp_path, python=tmp_path / "no" / "such" / "python")

    got = measure(sandbox, Target(qualified_name="sample.knotty", path="sample.py", score=0.0))

    assert got.present, "the target was not found, so the bed's interpreter is back"
    assert got.target != UNMEASURABLE
    assert got.target > 0
