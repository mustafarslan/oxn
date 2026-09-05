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

import re
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
    #: Frontmatter `tags`, and the prose after the closing `---`. Neither is read by the
    #: gate: they exist so retrieval ([ADR-0006](../../docs/adr/0006-retrieval-and-budgeting.md))
    #: has one ADR parser rather than a second one that disagrees with this about a
    #: malformed header. Carrying them is free -- `_frontmatter` reads the whole file
    #: either way -- and *indexing* them here would not be, which is why it happens
    #: elsewhere.
    tags: tuple[str, ...] = ()
    body: str = ""

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
    parsed = _frontmatter(path)
    if parsed is None:
        return _nygard(path, root)
    data, body = parsed
    ceilings = _mapping(data.get("constraints") or {}, "ceilings")
    return Decision(
        identifier=_text(data, "id") or path.stem,
        path=str(path.relative_to(root)),
        title=_text(data, "title"),
        status=_text(data, "status"),
        applies_to=_sequence(data, "applies-to"),
        ceilings={key: float(value) for key, value in ceilings.items()},
        tags=_sequence(data, "tags"),
        body=body,
    )


#: `adr-tools` numbers every decision in its filename, and the number is what a commit
#: citing "ADR-0009" names. Requiring it keeps a `README.md` sitting in the same directory
#: from being read as a decision -- an index is not a decision, and it would be a poor
#: retrieval target dressed as a good one.
_NUMBERED = re.compile(r"\d+[-_]")

#: A Nygard-style heading: `# 12. Use an event bus`, the number repeated from the filename.
_NYGARD_TITLE = re.compile(r"^#\s+(?:\d+\.\s*)?(.+?)\s*$", re.MULTILINE)

#: The `## Status` section's first non-empty line. `Accepted`, `Superseded by ADR-14`, ...
_NYGARD_STATUS = re.compile(r"^##\s*Status\s*$\n+(.+?)\s*$", re.MULTILINE | re.IGNORECASE)


def _nygard(path: Path, root: Path) -> Decision | None:
    """An ADR with no frontmatter: title from the first heading, status from `## Status`.

    Every external corpus OXN could find is this shape -- five of five, all created by
    `adr-tools`, none carrying a YAML header. Before this, `load_decisions` returned an
    empty list for all of them, which made the ADR corpus of
    [ADR-0006](../../../docs/adr/0006-retrieval-and-budgeting.md) section 5 unreadable by
    the one parser section 2 says there must be exactly one of.

    Such a decision has **no `applies-to`**, so it constrains nothing and tightens nothing;
    it is retrievable and never gated. The identifier is the filename's number, which is
    what a commit citing "ADR-0009" is naming.
    """
    if not _NUMBERED.match(path.name):
        return None
    text = path.read_text(errors="replace")
    title = _NYGARD_TITLE.search(text)
    if title is None:
        return None
    status = _NYGARD_STATUS.search(text)
    return Decision(
        identifier=path.stem.split("-")[0],
        path=str(path.relative_to(root)),
        title=title.group(1),
        status=status.group(1) if status else "",
        body=text,
    )


def _text(data: dict[str, Any], key: str) -> str:
    """Frontmatter is hand-typed, so missing, null and empty all mean the same thing."""
    return str(data.get(key) or "")


def _sequence(data: dict[str, Any], key: str) -> tuple[str, ...]:
    return tuple(str(item) for item in (data.get(key) or ()))


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    return {str(k): v for k, v in (data.get(key) or {}).items()}


def _frontmatter(path: Path) -> tuple[dict[str, Any], str] | None:
    """The YAML block between the first two `---` lines and the prose after it.

    Every failure returns None rather than raising: an ADR is prose with a header, and a
    broken one must not stop the gate that governs the code it describes.
    """
    text = path.read_text(errors="replace")
    if not text.startswith("---"):
        return None
    front, marker, body = text.partition("---")[2].partition("\n---")
    if not marker:
        return None
    try:
        import yaml

        data = yaml.safe_load(front)
    except Exception:  # noqa: BLE001 - see the docstring
        return None
    return (data, body.lstrip("-\n")) if isinstance(data, dict) else None


def adr_facts(
    facts: Facts, decisions: list[Decision], paths: list[str], *, whole_project: bool = False
) -> None:
    """Project decisions into relations, tightening ceilings the scope covers.

    `whole_project` says whether `paths` is the entire tree or a subset being checked. It
    gates the *unscoped* projection only, and the distinction is not academic: "this scope
    matches nothing" is a claim about the repository. Evaluated against a subset it is
    nearly always true and always meaningless -- the hook checks one file, so every ADR
    that does not happen to govern *that* file would be reported as governing nothing, on
    every edit. Ceiling tightening is unaffected: it is a per-file question either way.
    """
    for decision in decisions:
        if decision.enforceable:
            _project(facts, decision, paths, whole_project=whole_project)


def _project(facts: Facts, decision: Decision, paths: list[str], *, whole_project: bool) -> None:
    """One decision's rows. Separate from the loop because it is where the judgement is."""
    covered = [path for path in paths if _covers(decision, path)]
    facts.add("adr", (decision.identifier, decision.path, decision.title))
    if covered:
        facts.add("adr_scope", *[(decision.identifier, path) for path in covered])
    elif whole_project and decision.applies_to:
        # Derived at projection time rather than by a rule: `not adr_scope(Id, _)` needs an
        # existential a range-restricted body cannot bind, and a real Datalog backend would
        # introduce a helper relation in a lower stratum to say the same thing.
        #
        # `and decision.applies_to` because a decision that declared no scope has not
        # claimed to govern anything: there is no stale pattern to report, and reporting one
        # anyway greets every Nygard-style repository with an advisory per decision.
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
