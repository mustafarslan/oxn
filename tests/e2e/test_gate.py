"""Does the installed `oxn` give the right answer about a codebase?

Not "does it exit non-zero" -- that a gate can achieve by failing everything. Each test
here plants a shape whose number is known before OXN is asked, and checks that OXN names
the entity, the rule and that number.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from harness import FIXTURES, SAME_SHAPE, SAME_SHAPE_METRICS, Installed, plant

pytestmark = pytest.mark.e2e

LANGUAGES = sorted(SAME_SHAPE)


def _json(result) -> dict:
    """`check --json` prints a human report first; the JSON is the last object on stdout."""
    start = result.stdout.index("{")
    return json.loads(result.stdout[start:])


def test_a_tree_inside_every_ceiling_passes(installed: Installed, project: Path) -> None:
    for source in SAME_SHAPE.values():
        plant(source, project / "src")
    result = installed.run("check", "--json", cwd=project)
    assert result.returncode == 0, result.stdout
    assert _json(result)["violations"] == []


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("metric", sorted(SAME_SHAPE_METRICS))
def test_one_shape_measures_the_same_in_every_grammar(
    installed: Installed, project: Path, language: str, metric: str
) -> None:
    """Six grammars spell this shape six ways; the number is a property of the shape.

    Disagreement here means at least five of the six answers are wrong, whichever number
    turns out to be right.
    """
    plant(SAME_SHAPE[language], project / "src")
    result = installed.run("metrics", "src", "--sort-by", metric, "--json", cwd=project)
    assert result.returncode == 0, result.stderr
    entities = json.loads(result.stdout)["entities"]
    scored = [
        entity for entity in entities if entity["qualified_name"].lower().endswith("classify")
    ]
    assert len(scored) == 1, f"expected one `classify` in {language}, got {entities}"
    assert scored[0]["value"] == SAME_SHAPE_METRICS[metric]


def test_a_function_one_point_over_the_ceiling_blocks(installed: Installed, project: Path) -> None:
    """13 against a ceiling of 12, with the entity, the rule and the number all named."""
    plant(FIXTURES / "over_ceiling.py.txt", project / "src")
    result = installed.run("check", "--json", cwd=project)

    assert result.returncode == 2, f"a violation must block; got {result.returncode}"
    violations = _json(result)["violations"]
    assert len(violations) == 1, violations
    assert violations[0]["rule"] == "cognitive_complexity"
    assert violations[0]["value"] == 13.0
    assert violations[0]["ceiling"] == 12.0
    assert violations[0]["blocking"] is True
    assert violations[0]["entity"].endswith("over_ceiling.classify")


def test_the_increment_trail_adds_up_to_the_score(installed: Installed, project: Path) -> None:
    """The trail is the evidence for the number; if they disagree, one of them is a lie."""
    plant(FIXTURES / "over_ceiling.py.txt", project / "src")
    violation = _json(installed.run("check", "--json", cwd=project))["violations"][0]
    increments = [int(line.split("+", 1)[1].split(" ", 1)[0]) for line in violation["explanation"]]
    assert sum(increments) == violation["value"] == 13.0


@pytest.mark.parametrize("language", LANGUAGES)
def test_a_declared_ceiling_governs_every_language(
    installed: Installed, project: Path, language: str
) -> None:
    """Tighten the ceiling below the known score and the gate must block, in all six.

    The shape scores 9 everywhere; a ceiling of 8 turns the same file into a violation
    without editing it, which is what makes this a test of `oxn.yaml` rather than of the
    metric.
    """
    plant(SAME_SHAPE[language], project / "src")
    (project / "oxn.yaml").write_text("ceilings:\n  cognitive_complexity: 8\n")

    result = installed.run("check", "--json", cwd=project)
    assert result.returncode == 2, f"{language} did not block: {result.stdout}"
    violations = _json(result)["violations"]
    assert [v["rule"] for v in violations] == ["cognitive_complexity"]
    assert violations[0]["value"] == 9.0
    assert violations[0]["ceiling"] == 8.0
