"""Choosing what to repair, and where to read it from.

Separate from the loop that repairs it, because they are different questions and because
`scripts/dogfood.py` reached its own `file_sloc` ceiling holding both. The split has teeth:
everything here is parameterised by a `Bed`, and every hardcoded `ROOT` in it was a place the
harness would have measured one tree while working in another.

That was not hypothetical. `_trail_for` read `ROOT / target.path`, so asking for a Go bed's
plan found go-kit's files and then tried to open them inside this repository. `plan` took no
bed at all and listed OXN's own functions under `--bed go-kit`. Both were found by running
the command rather than by reading it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from beds import SELF, Bed

DIM = "\033[2m"
RESET = "\033[0m"


def say(message: str = "") -> None:
    print(message)


@dataclass
class Target:
    """A function over the ceiling."""

    qualified_name: str
    path: str
    score: float
    trail: list[str] = field(default_factory=list)

    @property
    def leaf(self) -> str:
        return self.qualified_name.rsplit(".", 1)[-1]


def select_targets(ceiling: int, limit: int, skip: set[str], where: Bed = SELF) -> list[Target]:
    """Functions whose cognitive complexity exceeds the ceiling, worst first.

    `where` used to be `src/oxn`, written into this function. A result measured only on the
    repository the tool was written for is a statement about that repository, which is why
    P11 names three beds and why this now takes one.
    """
    result = subprocess.run(
        [sys.executable, "-m", "oxn", "metrics", "--json", "--limit", "60", *where.sources],
        cwd=where.root,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    over = [
        row
        for row in payload.get("entities", [])
        if row["value"] > ceiling and row["qualified_name"] not in skip
    ]
    targets = [
        Target(qualified_name=row["qualified_name"], path=row["path"], score=row["value"])
        for row in sorted(over, key=_worst_first)[:limit]
    ]
    for target in targets:
        target.trail = _trail_for(target, where)
    return targets


def _worst_first(row: dict[str, Any]) -> tuple[float, str]:
    """Highest score first, ties broken by name -- and the tie-break is the load-bearing half.

    `oxn metrics` ranks by value and leaves equal scores in whatever order it found them, so
    taking the first `limit` rows gave a *different* target on consecutive runs: three calls
    to `plan --limit 1` returned `topological_order`, `_merge_split_classes` and
    `tsconfig_aliases`, all scoring 12.

    For repairing the worst function that is untidy. For an experiment it is fatal: two arms
    are only comparable on the same problems, and without this each one silently got its own.
    A pilot's first arm table read "none 100%, hybrid 0%" for exactly that reason -- an
    artifact of target assignment presented as a finding.
    """
    return (-row["value"], row["qualified_name"])


def _trail_for(target: Target, where: Bed = SELF) -> list[str]:
    """The increment trail: the explanation an agent is supposed to act on."""
    from oxn.languages import get_parser
    from oxn.metrics import cognitive_complexity
    from oxn.profiles import profile_for_path

    profile = profile_for_path(target.path)
    if profile is None:
        return []
    source = (where.root / target.path).read_bytes()
    tree = get_parser(profile.name).parse(source)
    for node, name in _iter_functions(tree.root_node, profile):
        if name == target.leaf:
            return cognitive_complexity(node, profile, function_name=name).explain()
    return []


def _iter_functions(root: Any, profile: Any) -> list[tuple[Any, str]]:
    found: list[tuple[Any, str]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        definition = profile.unwrap(node)
        if definition.type in profile.function_like:
            name = profile.entity_name(definition)
            if name:
                found.append((definition, name))
    return found


def function_span(path: Path, leaf: str, profile: Any) -> tuple[int, int] | None:
    """Byte range of a function *including* its decorators, for mechanical replacement.

    Splicing by byte range rather than applying a model-produced diff: LLM diffs mis-apply,
    and the graph builder already records the exact span.
    """
    from oxn.graph.builder import build_file
    from oxn.languages import get_parser

    source = path.read_bytes()
    tree = get_parser(profile.name).parse(source)
    parsed = build_file(path.name, source, profile, tree.root_node)
    for entity in parsed.entities:
        if entity.name == leaf and entity.kind.value in {"function", "method"}:
            return entity.start_byte, entity.end_byte
    return None


# ---- sandbox ------------------------------------------------------------------------------


# ---- the loop -----------------------------------------------------------------------------


def plan(ceiling: int, limit: int, where: Bed = SELF) -> None:
    """What the harness would attempt, without attempting it.

    `where` is not decoration: `plan` took no bed and silently planned against this
    repository, so `--bed go-kit` listed OXN's own functions. The same wrong-tree defect as
    the sandbox, one command along, and found the same way -- by running it.
    """
    for target in select_targets(ceiling, limit, skip=set(), where=where):
        say(f"  {target.score:5.0f}  {target.leaf:32} {DIM}{target.path}{RESET}")
        for line in target.trail[:3]:
            say(f"{DIM}          {line}{RESET}")
