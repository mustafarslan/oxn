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


def test_the_before_state_is_measured_in_the_bed_and_not_in_this_repository(tmp_path) -> None:
    """`score_before` was 999 while `score_after` was real, and that is worse than losing one.

    The before-state is taken before any sandbox exists, and `measure` then fell back to this
    project's root -- so an external bed's target path, which is relative to *its* corpus, was
    resolved here, matched nothing, and came back `UNMEASURABLE`. `improved` is `after <
    before`, so every candidate scored as an improvement over a file nobody had measured, and
    a run printed `999 -> 58` for a function that scores 63.

    Directional, not incidental: the broken value is the large one, so it cannot fail safe.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, "scripts")
    from gauntlet import UNMEASURABLE, measure
    from targets import Target

    corpus = tmp_path / "elsewhere"
    corpus.mkdir()
    (corpus / "mod.py").write_text(
        "def tangled(items):\n"
        "    out = 0\n"
        "    for item in items:\n"
        "        if item:\n"
        "            for part in item:\n"
        "                if part:\n"
        "                    out += 1\n"
        "    return out\n"
    )
    target = Target(qualified_name="mod.tangled", path="mod.py", score=0.0)

    in_bed = measure(None, target, root=corpus)
    assert in_bed.present and in_bed.target != UNMEASURABLE

    # The same call without a root looks in this repository, where `mod.py` does not exist.
    assert measure(None, target, root=Path(__file__).resolve().parent.parent).target == (
        UNMEASURABLE
    )


# ---- keeping what a run produced ---------------------------------------------------------


def test_a_rate_limited_endpoint_stops_the_run_instead_of_being_asked_again() -> None:
    """39 of 44 attempts in one run were the same rate-limit refusal, across twelve targets.

    A 429 is a cloud endpoint saying "retry later" in as many words. Asking it again with the
    next target gets the same answer, so the run produced twelve targets' worth of identical
    rows and nothing else -- and a log full of those is not a record of a model that cannot
    refactor, which is what it looks like to anything reading it afterwards.

    Typed, not text-matched: the message in a 429 is prose from someone else's server.
    """
    import sys

    sys.path.insert(0, "scripts")
    from attempt import _was_refused
    from oxn.llm import OllamaError, OllamaUnavailable

    assert _was_refused(OllamaUnavailable("HTTP 429 -- rate limited, retry later"))
    assert _was_refused(OllamaUnavailable("unreachable: timed out"))

    # A model that answered something unusable is a defect and must still be retried.
    assert not _was_refused(OllamaError("returned an empty completion"))
    assert not _was_refused(ValueError("something else entirely"))


def test_an_attempt_is_written_before_the_next_target_starts(tmp_path, monkeypatch) -> None:
    """The log was written once, after every target, and an interrupted run recorded nothing.

    Two interruptions on one afternoon -- an out-of-memory kill and a deliberate stop once
    the endpoint began refusing -- cost 44 attempts between them. The second threw away the
    only accepted repair an external bed had ever produced, `urlparse` 63 -> 6, which had sat
    in a list for forty minutes with nothing but the end of the loop between it and disk.

    Attempts are independent records. Nothing about one depends on the run finishing.
    """
    import json
    import sys

    sys.path.insert(0, "scripts")
    import runlog
    from runlog import Attempt, _append_log

    log = tmp_path / "log.jsonl"
    monkeypatch.setattr(runlog, "LOG", log)

    _append_log([Attempt("first", "a.py", 1, False, {})], announce=False)
    assert log.exists(), "the first target's row must be on disk before the second runs"

    _append_log([Attempt("second", "b.py", 1, True, {})], announce=False)
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert [row["target"] for row in rows] == ["first", "second"]
    assert rows[0]["endpoint_unavailable"] is False


def test_the_harness_names_the_actor_it_actually_defaults_to() -> None:
    """The docstring said `glm` writes and the default had been `kimi` since 2026-09-11.

    `--model`'s help text said `glm-5.3:cloud` too, while `oxn.llm.DEFAULT_MODEL` said
    otherwise. A file's own documentation arguing against its own default is worse than none:
    it gets believed. On 2026-09-18 it cost a six-target run to find out that glm fills
    whatever output budget it is given and is cut off mid-answer every time.

    The help text is built from the constants now, so this asserts they cannot disagree
    again rather than asserting today's model names.
    """
    import sys

    sys.path.insert(0, "scripts")
    import dogfood
    from oxn.llm import DEFAULT_JUDGE_MODEL, DEFAULT_MODEL

    help_text = dogfood._parser().format_help()
    assert DEFAULT_MODEL in help_text, "--model must name the default it actually uses"
    assert DEFAULT_JUDGE_MODEL in help_text

    # And the prose at the top of the file has to agree with them.
    assert DEFAULT_MODEL.split(":")[0].split("-")[0] in dogfood.__doc__


def test_the_report_does_not_read_the_unmeasurable_sentinel_as_a_score() -> None:
    """`urlparse` printed `999 -> 58` for a function that scores 63.

    `UNMEASURABLE` is 999 and deliberately enormous so that a file nobody could measure never
    wins a comparison that treats lower as better. Printing it as the starting complexity is
    the one place that largeness misleads instead of protecting, and the report did exactly
    that for every row written while `measure` was resolving an external bed's paths against
    this repository.

    Those rows stay in the log -- they were recorded honestly and the bug behind them is
    fixed. What changes is that a reader is no longer shown 999 as a complexity score.
    """
    import sys

    sys.path.insert(0, "scripts")
    from gauntlet import UNMEASURABLE
    from summary import _first_measured

    rows = [
        {"gauntlet": {"score_before": UNMEASURABLE, "score_after": 58.0}},
        {"gauntlet": {"score_before": 63.0, "score_after": 58.0}},
    ]
    assert _first_measured(rows) == 63.0

    # And a target that was never measured at all still reports nothing rather than 999.
    assert _first_measured([{"gauntlet": {"score_before": UNMEASURABLE}}]) is None
    assert _first_measured([]) is None


def test_a_repair_is_recognised_in_every_language_the_harness_declares_a_bed_for() -> None:
    """`_defines` was `def {name}(` and nothing else, so four of six beds could not accept
    a repair however good it was.

    The system prompt above `ACTOR_SYSTEM` carries a note about having once said "Python" for
    all six beds. That was fixed; this check was not, so the harness asked a Go model for Go
    and then refused the answer for not looking like Python. Measured on go-kit before the
    fix: six attempts across two targets, every one rejected here before the gauntlet ran,
    which reads in the log as a model that cannot refactor Go.

    Parsed rather than pattern-matched now, through the same `build_file` the graph builder
    uses, so "does this define X" is answered by whatever decides what an entity is
    everywhere else.
    """
    import sys

    sys.path.insert(0, "scripts")
    from actor import _defines
    from oxn.profiles import get_profile

    written = {
        "go": ("func TraceEndpoint(name string) error {\n\treturn nil\n}\n", "TraceEndpoint"),
        "rust": ("fn try_find_iter_at(&self, x: u8) -> bool {\n    true\n}\n", "try_find_iter_at"),
        "typescript": ("function handleRequest(a: string): void {\n  return;\n}\n", "handleRequest"),
        "java": ("public class C {\n  void doWork(int a) { }\n}\n", "doWork"),
        "python": ("def repair(x):\n    return x\n", "repair"),
    }
    for language, (source, name) in written.items():
        profile = get_profile(language)
        assert _defines(source, name, profile), f"{language}: a real definition was refused"
        assert not _defines(source, "absent", profile), f"{language}: accepted a missing name"


def test_the_judge_is_shown_the_language_it_is_judging() -> None:
    """The judge's fence said ```python for every bed -- the actor's prompt's old bug, left
    in place one function below it. A Go refactoring was labelled Python to the model asked
    whether it was a real simplification."""
    import sys

    sys.path.insert(0, "scripts")
    from actor import JUDGE_PROMPT, Review

    filled = JUDGE_PROMPT.format(
        before=44, after=12, original="func a() {}", candidate="func a() { b() }", language="go"
    )
    assert "```go" in filled
    assert "```python" not in filled
    assert Review("x", "y", 1.0, 2.0).language == "python", "the default stays python"
