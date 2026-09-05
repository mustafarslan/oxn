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


def test_the_body_and_tags_travel_with_the_decision(adr_dir) -> None:
    """One parser, so the document the gate enforces is the document retrieval ranks.

    ADR-0006 needs the prose and the tags. It gets them from here rather than from a
    second frontmatter reader, which would be free to disagree with this one about a
    malformed header.
    """
    (adr_dir / "docs" / "adr" / "0043-y.md").write_text(
        "---\nid: ADR-0043\ntitle: T\nstatus: accepted\ntags: [parser, retry]\n---\n\n"
        "# ADR-0043\n\nThe body, which the gate never reads.\n"
    )
    (decision,) = load_decisions(adr_dir)
    assert decision.tags == ("parser", "retry")
    assert decision.body.startswith("# ADR-0043")
    assert "the gate never reads" in decision.body


def test_a_decision_without_tags_or_a_body_still_parses(adr_dir) -> None:
    _write(adr_dir)
    (decision,) = load_decisions(adr_dir)
    assert decision.tags == ()
    assert decision.body.strip() == "# Body"


def test_a_malformed_adr_is_skipped_and_never_fatal(adr_dir) -> None:
    """A broken document must not stop the gate: it is prose with a header, not code."""
    (adr_dir / "docs" / "adr" / "broken.md").write_text("---\nnot: [valid\n---\n")
    (adr_dir / "docs" / "adr" / "noheader.md").write_text("# Just a heading\n")
    _write(adr_dir)
    assert [d.identifier for d in load_decisions(adr_dir)] == ["ADR-0042"]


NYGARD = """# 9. Client Timeouts

Date: 2024-06-21

## Status

Accepted

## Context

The request loop may still apply a request after its caller has gone.
"""


def test_an_adr_with_no_frontmatter_is_still_a_decision(adr_dir) -> None:
    """Every external ADR corpus OXN could find is this shape: five of five, all made by
    `adr-tools`, none carrying a YAML header. Skipping them made the corpus ADR-0006
    section 5 needs unreadable by the one parser section 2 says there must be one of."""
    (adr_dir / "docs" / "adr" / "0009-client-timeouts.md").write_text(NYGARD)
    (decision,) = load_decisions(adr_dir)
    assert decision.identifier == "0009"  # what a commit citing "ADR-0009" names
    assert decision.title == "Client Timeouts"  # the heading's own number stripped
    assert decision.status == "Accepted"
    assert "caller has gone" in decision.body
    assert decision.applies_to == ()  # retrievable, and gated by nothing


def test_an_unnumbered_file_beside_the_decisions_is_not_one(adr_dir) -> None:
    """An index is not a decision, and would be a poor retrieval target dressed as a good
    one. `adr-tools` numbers every decision it writes, so the number is the discriminator."""
    (adr_dir / "docs" / "adr" / "README.md").write_text("# Architecture Decision Records\n")
    (adr_dir / "docs" / "adr" / "0009-client-timeouts.md").write_text(NYGARD)
    assert [d.identifier for d in load_decisions(adr_dir)] == ["0009"]


def test_a_decision_that_declared_no_scope_is_not_reported_as_governing_nothing(
    adr_dir,
) -> None:
    """A claim that "your scope matches nothing" is about a scope somebody wrote. An ADR with no
    `applies-to` has not claimed to govern anything, so there is nothing to report -- and
    reporting it would greet every Nygard-style repository with one advisory per decision
    on its first `--deep` run."""
    (adr_dir / "docs" / "adr" / "0009-client-timeouts.md").write_text(NYGARD)
    facts = Facts()
    adr_facts(facts, load_decisions(adr_dir), ["src/parser/p.py"], whole_project=True)
    assert not facts.get("adr_unscoped")


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
    adr_facts(facts, load_decisions(adr_dir), ["src/parser/p.py"], whole_project=True)
    (finding,) = evaluate([unscoped_rule()], facts)
    assert finding.entity == "ADR-0042"
    assert finding.rule == "adr:unscoped"


def test_it_is_advisory_because_an_adr_may_predate_its_code(adr_dir) -> None:
    _write(adr_dir, scope='["src/server/**"]')
    facts = Facts()
    adr_facts(facts, load_decisions(adr_dir), ["src/parser/p.py"], whole_project=True)
    assert evaluate([unscoped_rule()], facts)[0].blocking is False


def test_checking_a_subset_never_reports_a_scope_as_empty(adr_dir) -> None:
    """ "This governs nothing" is a claim about the repository, not about what you checked.

    The hook checks a single file. Evaluated against that subset, every ADR not governing
    *that* file looks unscoped -- so a per-edit hook would have reported OXN's own ADRs as
    dead on almost every save. Measured before the fix: checking `src/oxn/check.py` alone
    reported ADR-0002 and ADR-0004 as governing nothing.
    """
    _write(adr_dir, scope='["src/parser/*"]')
    facts = Facts()
    adr_facts(facts, load_decisions(adr_dir), ["src/other/o.py"], whole_project=False)
    assert evaluate([unscoped_rule()], facts) == []


def test_a_scope_that_matches_produces_no_finding(adr_dir) -> None:
    _write(adr_dir)
    facts = _facts_with_ceiling()
    adr_facts(facts, load_decisions(adr_dir), ["src/parser/p.py"], whole_project=True)
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
