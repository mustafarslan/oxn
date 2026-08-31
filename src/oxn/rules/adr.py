"""Compile ADR frontmatter into the same facts and rules `oxn.yaml` compiles into.

This is what makes "ADRs as a constraint source" a contribution rather than a slogan. An
architecture decision that compiles to a hand-written Python branch has contributed nothing
a config file could not; one that compiles to the *same* `Rule` shape as `oxn.yaml`, joined
against the same relations, is genuinely data.

Frontmatter already carries `applies-to`. A decision adds `constraints`:

```yaml
constraints:
  ceilings: {cognitive_complexity: 8}
```

**An ADR may tighten a ceiling and never loosen one.** The effective limit for a file is the
smallest of the project's and every ADR's covering it. A decision that could relax a
project-wide gate would be a way to switch the gate off one document at a time, which is
the same failure the baseline is carefully not.

**A scope matching no files is reported, not ignored.** A constraint governing nothing is
almost always a stale path, and this project has been bitten three times in one cycle by
patterns that silently matched nothing -- `shutil.ignore_patterns` matching basenames only,
a Java wildcard whose grammar node was named rather than anonymous, and a corpus path that
three shell arguments collapsed into one. The finding is advisory rather than blocking
because an ADR may legitimately predate the code it governs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Any

from oxn.rules.model import Atom, Head, Rule, Var

if TYPE_CHECKING:  # pragma: no cover
    from oxn.rules.model import Facts

#: Where decisions live unless `oxn.yaml` says otherwise.
DEFAULT_ADR_DIR = Path("docs/adr")


@dataclass(frozen=True, slots=True)
class Decision:
    """One ADR, reduced to the parts a gate can act on."""

    identifier: str
    path: str
    title: str
    status: str
    applies_to: tuple[str, ...] = ()
    ceilings: dict[str, float] = field(default_factory=dict)

    @property
    def enforceable(self) -> bool:
        """Only an accepted decision constrains anything.

        A proposed or superseded ADR is a document, not a rule. Enforcing one would make
        the gate depend on whether somebody remembered to change a status line -- which is
        exactly the sort of quiet coupling that makes a gate untrustworthy.
        """
        return self.status.strip().lower() == "accepted"


def load_decisions(root: Path, directory: Path | None = None) -> list[Decision]:
    """Every ADR under `directory`, parsed. Malformed frontmatter is skipped, not fatal."""
    base = root / (directory or DEFAULT_ADR_DIR)
    if not base.is_dir():
        return []
    found = [_parse(path, root) for path in sorted(base.glob("*.md"))]
    return [decision for decision in found if decision is not None]


def _parse(path: Path, root: Path) -> Decision | None:
    data = _frontmatter(path)
    if data is None:
        return None
    constraints = data.get("constraints") or {}
    return Decision(
        identifier=str(data.get("id") or path.stem),
        path=str(path.relative_to(root)),
        title=str(data.get("title") or ""),
        status=str(data.get("status") or ""),
        applies_to=tuple(data.get("applies-to") or ()),
        ceilings={str(k): float(v) for k, v in (constraints.get("ceilings") or {}).items()},
    )


def _frontmatter(path: Path) -> dict[str, Any] | None:
    """The YAML block between the first two `---` lines, or None if there isn't one.

    Every failure returns None rather than raising: an ADR is prose with a header, and a
    broken one must not stop the gate that governs the code it describes.
    """
    text = path.read_text(errors="replace")
    if not text.startswith("---"):
        return None
    front, marker, _ = text.partition("---")[2].partition("\n---")
    if not marker:
        return None
    try:
        import yaml

        data = yaml.safe_load(front)
    except Exception:  # noqa: BLE001 - see the docstring
        return None
    return data if isinstance(data, dict) else None


def adr_facts(facts: Facts, decisions: list[Decision], paths: list[str]) -> None:
    """Project decisions into relations, tightening ceilings the scope covers."""
    for decision in decisions:
        if not decision.enforceable:
            continue
        covered = [path for path in paths if _covers(decision, path)]
        facts.add("adr", (decision.identifier, decision.path, decision.title))
        if covered:
            facts.add("adr_scope", *[(decision.identifier, path) for path in covered])
        else:
            # Derived at projection time rather than by a rule: `not adr_scope(Id, _)` needs
            # an existential a range-restricted body cannot bind, and a real Datalog backend
            # would introduce a helper relation in a lower stratum to say the same thing.
            facts.add("adr_unscoped", (decision.identifier, decision.path))
        _tighten(facts, decision, covered)


def _covers(decision: Decision, path: str) -> bool:
    return any(fnmatch(path, pattern) for pattern in decision.applies_to)


def _tighten(facts: Facts, decision: Decision, covered: list[str]) -> None:
    """Replace a ceiling row where this decision is stricter than what is already there."""
    scope = set(covered)
    for rule_name, limit in decision.ceilings.items():
        for row in _looser_rows(facts, rule_name, scope, limit):
            facts.relations["ceiling"].discard(row)
            facts.add("ceiling", (rule_name, row[1], limit))
            facts.add("adr_tightened", (decision.identifier, rule_name, row[1], limit))


def _looser_rows(
    facts: Facts, rule_name: str, scope: set[str], limit: float
) -> list[tuple[object, ...]]:
    """Ceiling rows this decision covers and is stricter than."""
    return [
        row
        for row in facts.get("ceiling")
        if row[0] == rule_name and row[1] in scope and limit < row[2]
    ]


def unscoped_rule() -> Rule:
    """Advisory: this decision's `applies-to` matches nothing in the tree."""
    return Rule(
        name="adr:unscoped",
        body=(Atom("adr_unscoped", (Var("Id"), Var("Doc"))),),
        head=Head(
            path=Var("Doc"),
            entity=Var("Id"),
            blocking=False,
            detail="applies-to matches no file in this tree",
        ),
    )
