"""`oxn seam`: split a scope into lanes that can be worked on in parallel (DX-X1).

Deterministic and pure: the caller builds the file graph and passes it in. The steps follow
the dux plan (`docs/next_dux_update.md` §6, DX-X1, in Aero and tower):

1. the file graph over the scope plus one hop;
2. the repo's commons globs taken out;
3. strongly connected components condensed, so a cycle is never split;
4. hubs -- files imported from at least two otherwise-separate parts -- extracted as
   contracts, which go in a contract lane that runs first;
5. the connected components of what remains become lanes, and a lane under ``min_lane``
   files is merged into the neighbour it shares the most edges with;
6. every remaining cross-lane edge adds its target to the contracts;
7. the verdict is ``solo`` when any reason holds, and every reason is listed.

The output is schema v1 of the contract Aero reads (`docs/next_dux_update.md` §3.5).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping  # noqa: TC003 - runtime alias
from dataclasses import dataclass, field
from fnmatch import fnmatch
from posixpath import dirname

from oxn.graph.algos import condensation

#: Cross-lane edges per lane above which the parts are too entangled to fan out.
MAX_CROSS_EDGES_PER_LANE = 2
#: Unresolved imports above this share mean the graph can't be trusted to seam.
MAX_UNRESOLVED_SHARE = 0.05


@dataclass(frozen=True)
class SeamInput:
    """What `seam` weighs. ``edges`` maps a repo file to the repo files it imports."""

    edges: Mapping[str, frozenset[str]]
    scope: frozenset[str]
    commons: tuple[str, ...] = ()
    lanes_max: int = 8
    min_lane: int = 3
    unresolved_share: float = 0.0
    languages_seen: tuple[str, ...] = ()
    languages_unparsed: tuple[str, ...] = ()


@dataclass
class _Plan:
    """The working state: which part each scope file is in, and the hubs found."""

    lane_of: dict[str, int] = field(default_factory=dict)
    hubs: dict[str, int] = field(default_factory=dict)
    hub_in_cycle: bool = False
    cyclic: set[str] = field(default_factory=set)


def _undirected(edges: Mapping[str, Iterable[str]], keep: set[str], unit: Mapping[str, str]) -> dict[str, set[str]]:
    """Both directions of every edge whose ends are both kept, and each kept file joined to its unit's anchor."""
    graph: dict[str, set[str]] = {node: set() for node in keep}
    pairs = [(node, unit.get(node, node)) for node in keep]
    pairs += [(source, target) for source in keep for target in edges.get(source, ())]
    for a, b in pairs:
        if a != b and b in keep:
            graph[a].add(b)
            graph[b].add(a)
    return graph


def _components(graph: Mapping[str, set[str]]) -> dict[str, int]:
    """Connected components, numbered in the sorted order of their first node."""
    part: dict[str, int] = {}
    for start in sorted(graph):
        if start in part:
            continue
        number = len(set(part.values()))
        part[start] = number
        queue = deque([start])
        while queue:
            for neighbour in sorted(graph[queue.popleft()]):
                if neighbour not in part:
                    part[neighbour] = number
                    queue.append(neighbour)
    return part


def _graph(seam_input: SeamInput) -> dict[str, frozenset[str]]:
    """Steps 1 and 2: the scope plus one hop, without the commons."""

    def common(path: str) -> bool:
        return any(fnmatch(path, pattern) for pattern in seam_input.commons)

    nodes = set(seam_input.scope)
    for source, targets in seam_input.edges.items():
        if source in seam_input.scope:
            nodes.update(targets)
        elif targets & seam_input.scope:
            nodes.add(source)
    nodes = {node for node in nodes if not common(node)}
    return {
        node: frozenset(target for target in seam_input.edges.get(node, ()) if target in nodes)
        for node in sorted(nodes)
    }


def _package(path: str) -> str | None:
    """A Go file's package (its folder): its files share one namespace, so they never move apart."""
    return "go:" + dirname(path) if path.endswith(".go") else None


def _units(graph: Mapping[str, frozenset[str]], plan: _Plan) -> dict[str, str]:
    """Step 3: each file's unit, named by its smallest member: a dependency cycle, joined with its Go package.

    A unit moves as one. ``plan.cyclic`` records the units that hold a real cycle, which a hub must not be in.
    """
    membership, _, components = condensation(graph)
    root: dict[str, str] = {}

    def find(node: str) -> str:
        while root.get(node, node) != node:
            node = root[node]
        return node

    groups: dict[str, list[str]] = {}
    for node in graph:
        groups.setdefault(f"scc:{membership[node]}", []).append(node)
        package = _package(node)
        if package:
            groups.setdefault(package, []).append(node)
    for members in groups.values():
        first = find(min(members))
        for member in members:
            root[find(member)] = first
    unit = {node: find(node) for node in graph}
    plan.cyclic = {unit[component[0]] for component in components if len(component) > 1}
    return unit


def _importers(graph: Mapping[str, frozenset[str]], unit: Mapping[str, str]) -> dict[str, set[str]]:
    """Each unit's importing files from other units."""
    importers: dict[str, set[str]] = {}
    for source, targets in graph.items():
        for target in (t for t in targets if unit[t] != unit[source]):
            importers.setdefault(unit[target], set()).add(source)
    return importers


def _splits(graph: Mapping[str, frozenset[str]], unit: Mapping[str, str], gone: set[str], importers: set[str]) -> bool:
    """Whether ``importers`` fall in two or more parts once the units in ``gone`` are taken out."""
    part = _components(_undirected(graph, {node for node in graph if unit[node] not in gone}, unit))
    return len({part[source] for source in importers if source in part}) >= 2


def _find_hubs(graph: Mapping[str, frozenset[str]], unit: Mapping[str, str], plan: _Plan) -> None:
    """Step 4: a unit whose importers fall in two or more parts once it is taken out.

    Repeated until nothing changes, since taking one hub out can reveal another.
    """
    importers = _importers(graph, unit)
    found: set[str] = set()
    grew = True
    while grew:
        new = {c for c in sorted(set(importers) - found) if _splits(graph, unit, found | {c}, importers[c])}
        found |= new
        grew = bool(new)
    plan.hubs = {node: len(importers[unit[node]]) for node in graph if unit[node] in found}
    plan.hub_in_cycle = bool(found & plan.cyclic)


def _edges_between(graph: Mapping[str, frozenset[str]], lane_of: Mapping[str, int]) -> dict[tuple[int, int], int]:
    """How many file edges join each pair of lanes, either way."""
    counts: dict[tuple[int, int], int] = {}
    for source, targets in graph.items():
        for target in targets:
            a, b = lane_of.get(source), lane_of.get(target)
            if a is not None and b is not None and a != b:
                key = (min(a, b), max(a, b))
                counts[key] = counts.get(key, 0) + 1
    return counts


def _nearest_by_path(lane_of: Mapping[str, int], small: int) -> int | None:
    """The other lane whose first file shares the longest folder prefix with ``small``'s: for lanes with no edge between them."""
    firsts: dict[int, str] = {}
    for node in sorted(lane_of):
        firsts.setdefault(lane_of[node], node)
    mine = firsts.get(small, "")

    def shared(other: str) -> int:
        common = 0
        for a, b in zip(dirname(mine).split("/"), dirname(other).split("/"), strict=False):
            if a != b:
                break
            common += 1
        return common

    others = sorted((-shared(path), lane) for lane, path in firsts.items() if lane != small)
    return others[0][1] if others else None


def _merge_one(graph: Mapping[str, frozenset[str]], lane_of: dict[str, int], small: int) -> bool:
    """Fold lane ``small`` into the neighbour it shares the most edges with, else the nearest folder's lane; False if it is the only one."""
    counts = _edges_between(graph, lane_of)
    neighbours = sorted(
        ((count, -other) for (a, b), count in counts.items() for other in (a, b) if small in (a, b) and other != small),
        reverse=True,
    )
    into = -neighbours[0][1] if neighbours else _nearest_by_path(lane_of, small)
    if into is None:
        return False
    for node, lane in lane_of.items():
        if lane == small:
            lane_of[node] = into
    return True


def _lane_sizes(lane_of: Mapping[str, int]) -> list[tuple[int, int]]:
    """``(size, lane)`` for each lane, smallest first."""
    count: dict[int, int] = {}
    for lane in lane_of.values():
        count[lane] = count.get(lane, 0) + 1
    return sorted((size, lane) for lane, size in count.items())


def _next_merge(lane_of: Mapping[str, int], seam_input: SeamInput, stuck: set[int]) -> int | None:
    """The smallest lane that must merge: under ``min_lane`` files, or any while there are more lanes than ``lanes_max``."""
    lanes = _lane_sizes(lane_of)
    over = len(lanes) > max(seam_input.lanes_max, 1)
    candidates = [lane for size, lane in lanes if (over or size < seam_input.min_lane) and lane not in stuck]
    return candidates[0] if candidates else None


def _form_lanes(graph: Mapping[str, frozenset[str]], seam_input: SeamInput, plan: _Plan, unit: Mapping[str, str]) -> None:
    """Step 5: components of the scope without hubs, then small lanes (and lanes past the cap) merged in.

    A lane with no edge to any other joins the lane of the nearest folder.
    """
    keep = {node for node in seam_input.scope if node in graph and node not in plan.hubs}
    plan.lane_of = _components(_undirected(graph, keep, unit))
    stuck: set[int] = set()
    while (lane := _next_merge(plan.lane_of, seam_input, stuck)) is not None:
        if not _merge_one(graph, plan.lane_of, lane):
            stuck.add(lane)


def _lane_name(files: list[str], taken: set[str]) -> str:
    """The deepest folder every file shares, made unique; ``lane`` at the root."""
    folder = dirname(files[0])
    while folder and not all(path.startswith(folder + "/") for path in files):
        folder = dirname(folder)
    base = (folder.rsplit("/", 1)[-1] or "lane") if folder else "lane"
    name, suffix = base, 2
    while name in taken:
        name, suffix = f"{base}-{suffix}", suffix + 1
    taken.add(name)
    return name


def _widest(path: str, others: set[str]) -> str:
    """The top-most ``folder/**`` above ``path`` that holds none of ``others``, else ``path`` itself."""
    best, folder = path, dirname(path)
    while folder and not any(other.startswith(folder + "/") for other in others):
        best, folder = folder + "/**", dirname(folder)
    return best


def _globs(files: list[str], every: Iterable[str]) -> list[str]:
    """``folder/**`` for each top-most folder whose scope files are all this lane's; files otherwise."""
    others = set(every) - set(files)
    globs = sorted({_widest(path, others) for path in files})
    folders = [glob[:-2] for glob in globs if glob.endswith("/**")]
    return [glob for glob in globs if not any(glob != f + "**" and glob.startswith(f) for f in folders)]


def _reasons(seam_input: SeamInput, plan: _Plan, lanes: int, cross: int) -> list[str]:
    """Step 7: every reason the answer is solo, in a fixed order."""
    reasons: list[str] = []
    if lanes < 2:
        reasons.append(f"fewer than 2 lanes ({lanes})")
    if lanes and cross / lanes > MAX_CROSS_EDGES_PER_LANE:
        reasons.append(f"{cross} cross-lane edges for {lanes} lanes (over {MAX_CROSS_EDGES_PER_LANE} per lane)")
    if plan.hub_in_cycle:
        reasons.append("a hub is inside a dependency cycle")
    if seam_input.languages_unparsed:
        reasons.append("unparsed languages in scope: " + ", ".join(sorted(seam_input.languages_unparsed)))
    if seam_input.unresolved_share > MAX_UNRESOLVED_SHARE:
        reasons.append(f"unresolved import share {seam_input.unresolved_share:.2f} is over {MAX_UNRESOLVED_SHARE}")
    return reasons


def _cross_contracts(graph: Mapping[str, frozenset[str]], plan: _Plan, contracts: dict[str, str]) -> int:
    """Step 6: every cross-lane edge's target becomes a contract; the number of such edges."""
    cross = 0
    for source, targets in graph.items():
        for target in targets:
            lanes = (plan.lane_of.get(source), plan.lane_of.get(target))
            if None not in lanes and lanes[0] != lanes[1]:
                cross += 1
                contracts.setdefault(target, f"edge {source} -> {target}")
    return cross


def _lanes(plan: _Plan, contracts: Mapping[str, str]) -> list[dict[str, object]]:
    """The lanes as Aero reads them: a name, the globs that cover them, and the contract lane they wait for."""
    by_lane: dict[int, list[str]] = {}
    for node, lane in plan.lane_of.items():
        if node not in contracts:
            by_lane.setdefault(lane, []).append(node)
    taken: set[str] = set()
    depends = ["contract"] if contracts else []
    groups = sorted((sorted(files) for files in by_lane.values()), key=lambda files: files[0])
    return [{"name": _lane_name(files, taken), "globs": _globs(files, plan.lane_of), "depends_on": depends} for files in groups]


def seam(seam_input: SeamInput) -> dict[str, object]:
    """The schema v1 answer for ``seam_input``: same input, same output."""
    graph = _graph(seam_input)
    plan = _Plan()
    unit = _units(graph, plan)
    _find_hubs(graph, unit, plan)
    _form_lanes(graph, seam_input, plan, unit)
    contracts: dict[str, str] = {hub: "hub" for hub in plan.hubs if hub in seam_input.scope}
    cross = _cross_contracts(graph, plan, contracts)
    lanes = _lanes(plan, contracts)
    reasons = _reasons(seam_input, plan, len(lanes), cross)
    return {
        "v": 1,
        "verdict": "solo" if reasons else "fan_out",
        "reasons": reasons,
        "lanes": lanes,
        "contracts": [{"path": path, "why": why} for path, why in sorted(contracts.items())],
        "hubs": [{"path": path, "fan_in": fan_in} for path, fan_in in sorted(plan.hubs.items())],
        "cross_lane_edges": cross,
        "coverage": {
            "languages_seen": sorted(seam_input.languages_seen),
            "languages_unparsed": sorted(seam_input.languages_unparsed),
            "unresolved_share": round(seam_input.unresolved_share, 4),
        },
    }
