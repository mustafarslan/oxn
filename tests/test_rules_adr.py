"""ADR frontmatter compiled to the same facts and rules `oxn.yaml` compiles to.

This is what makes "ADRs as a constraint source" a contribution rather than a slogan: a
decision that compiled to a hand-written branch would have contributed nothing a config
file could not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oxn.rules.adr import adr_facts, load_decisions, unscoped_rule
from oxn.rules.engine import evaluate
from oxn.rules.model import Facts

ADR = """---
id: ADR-0042
title: Keep the parser simple
status: {status}
applies-to: {scope}
constraints:
  ceilings: {{cognitive_complexity: {limit}}}
---

# Body
"""


@pytest.fixture
def adr_dir(tmp_path):
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    return tmp_path


def _write(root: Path, name: str = "0042-x.md", **kwargs) -> None:
    text = ADR.format(
        status=kwargs.get("status", "accepted"),
        scope=kwargs.get("scope", '["src/parser/*"]'),
        limit=kwargs.get("limit", 8),
    )
    (root / "docs" / "adr" / name).write_text(text)


def _facts_with_ceiling(limit: float = 12.0) -> Facts:
    facts = Facts()
    facts.add("ceiling", ("cognitive_complexity", "src/parser/p.py", limit))
    return facts


# ---- parsing ----------------------------------------------------------------------------


def test_frontmatter_becomes_a_decision(adr_dir) -> None:
    _write(adr_dir)
    (decision,) = load_decisions(adr_dir)
    assert decision.identifier == "ADR-0042"
    assert decision.applies_to == ("src/parser/*",)
    assert decision.ceilings == {"cognitive_complexity": 8.0}


def test_a_malformed_adr_is_skipped_and_never_fatal(adr_dir) -> None:
    """A broken document must not stop the gate: it is prose with a header, not code."""
    (adr_dir / "docs" / "adr" / "broken.md").write_text("---\nnot: [valid\n---\n")
    (adr_dir / "docs" / "adr" / "noheader.md").write_text("# Just a heading\n")
    _write(adr_dir)
    assert [d.identifier for d in load_decisions(adr_dir)] == ["ADR-0042"]


def test_only_an_accepted_decision_constrains_anything(adr_dir) -> None:
    """A proposed or superseded ADR is a document. Enforcing one would make the gate
    depend on whether somebody remembered to update a status line."""
    _write(adr_dir, status="proposed")
    facts = _facts_with_ceiling()
    adr_facts(facts, load_decisions(adr_dir), ["src/parser/p.py"])
    assert ("cognitive_complexity", "src/parser/p.py", 12.0) in facts.get("ceiling")
    assert not facts.get("adr")


# ---- ceilings ---------------------------------------------------------------------------


def test_an_adr_tightens_a_ceiling_within_its_scope(adr_dir) -> None:
    _write(adr_dir, limit=8)
    facts = _facts_with_ceiling(12.0)
    adr_facts(facts, load_decisions(adr_dir), ["src/parser/p.py"])
    assert ("cognitive_complexity", "src/parser/p.py", 8.0) in facts.get("ceiling")
    assert ("cognitive_complexity", "src/parser/p.py", 12.0) not in facts.get("ceiling")


def test_an_adr_may_never_loosen_one(adr_dir) -> None:
    """Otherwise a decision is a way to switch the gate off one document at a time."""
    _write(adr_dir, limit=40)
    facts = _facts_with_ceiling(12.0)
    adr_facts(facts, load_decisions(adr_dir), ["src/parser/p.py"])
    assert ("cognitive_complexity", "src/parser/p.py", 12.0) in facts.get("ceiling")
    assert ("cognitive_complexity", "src/parser/p.py", 40.0) not in facts.get("ceiling")


def test_the_tightening_stops_at_the_scope_boundary(adr_dir) -> None:
    _write(adr_dir, limit=8, scope='["src/parser/*"]')
    facts = Facts()
    facts.add("ceiling", ("cognitive_complexity", "src/parser/p.py", 12.0))
    facts.add("ceiling", ("cognitive_complexity", "src/other/o.py", 12.0))
    adr_facts(facts, load_decisions(adr_dir), ["src/parser/p.py", "src/other/o.py"])
    limits = {(row[1], row[2]) for row in facts.get("ceiling")}
    assert limits == {("src/parser/p.py", 8.0), ("src/other/o.py", 12.0)}


# ---- a constraint that governs nothing ---------------------------------------------------


def test_a_scope_matching_no_file_is_reported(adr_dir) -> None:
    """A constraint governing nothing is almost always a stale path.

    Three bugs in one cycle came from patterns that silently matched nothing, so this one
    is surfaced rather than trusted -- and it caught three of OXN's own five ADRs, whose
    `applies-to` still said `oxn/**` after the package moved under `src/`.
    """
    _write(adr_dir, scope='["nowhere/**"]')
    facts = Facts()
    adr_facts(facts, load_decisions(adr_dir), ["src/parser/p.py"])
    (finding,) = evaluate([unscoped_rule()], facts)
    assert finding.entity == "ADR-0042"
    assert finding.rule == "adr:unscoped"


def test_it_is_advisory_because_an_adr_may_predate_its_code(adr_dir) -> None:
    _write(adr_dir, scope='["src/server/**"]')
    facts = Facts()
    adr_facts(facts, load_decisions(adr_dir), ["src/parser/p.py"])
    assert evaluate([unscoped_rule()], facts)[0].blocking is False


def test_a_scope_that_matches_produces_no_finding(adr_dir) -> None:
    _write(adr_dir)
    facts = _facts_with_ceiling()
    adr_facts(facts, load_decisions(adr_dir), ["src/parser/p.py"])
    assert evaluate([unscoped_rule()], facts) == []


def test_decisions_without_an_adr_directory_are_simply_absent(tmp_path) -> None:
    assert load_decisions(tmp_path) == []


# ---- the real thing ----------------------------------------------------------------------


def test_oxns_own_decisions_parse() -> None:
    """The compiler must handle the documents this repository actually ships."""
    decisions = load_decisions(Path())
    assert len(decisions) >= 5
    assert all(d.identifier.startswith("ADR-") for d in decisions)
    assert all(d.enforceable for d in decisions), "every shipped ADR is accepted"
