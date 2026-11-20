"""`oxn seam` (DX-X1): the plan's fixtures and properties, on the pure function."""

from __future__ import annotations

import json
import random

from oxn.graph.seam import SeamInput, seam


def _input(edges: dict[str, set[str]], scope: set[str] | None = None, **extra: object) -> SeamInput:
    files = set(edges) | {target for targets in edges.values() for target in targets}
    return SeamInput(
        edges={source: frozenset(targets) for source, targets in edges.items()},
        scope=frozenset(scope if scope is not None else files),
        **extra,  # type: ignore[arg-type]
    )


def _pkg(name: str, size: int, imports: set[str]) -> dict[str, set[str]]:
    """``size`` files in folder ``name``, chained, the first importing ``imports``."""
    files = [f"{name}/m{index}.py" for index in range(size)]
    edges = {path: {files[index + 1]} for index, path in enumerate(files[:-1])}
    edges.setdefault(files[-1], set())
    edges[files[0]] = edges[files[0]] | imports
    return edges


def test_two_packages_sharing_types_give_a_contract_lane_and_two_lanes() -> None:
    edges = {**_pkg("pkga", 3, {"types.py"}), **_pkg("pkgb", 3, {"types.py"}), "types.py": set()}
    result = seam(_input(edges))
    assert result["verdict"] == "fan_out", result["reasons"]
    assert result["contracts"] == [{"path": "types.py", "why": "hub"}]
    assert result["lanes"] == [
        {"name": "pkga", "globs": ["pkga/**"], "depends_on": ["contract"]},
        {"name": "pkgb", "globs": ["pkgb/**"], "depends_on": ["contract"]},
    ]
    assert result["hubs"] == [{"path": "types.py", "fan_in": 2}]


def test_a_cyclic_repository_stays_solo() -> None:
    edges = {"a.py": {"b.py"}, "b.py": {"c.py"}, "c.py": {"a.py"}, "d.py": {"a.py"}, "e.py": {"d.py"}}
    result = seam(_input(edges))
    assert result["verdict"] == "solo"
    assert any(reason.startswith("fewer than 2 lanes") for reason in result["reasons"])


def test_a_swift_scope_stays_solo() -> None:
    edges = {**_pkg("pkga", 3, set()), **_pkg("pkgb", 3, set())}
    result = seam(_input(edges, languages_unparsed=("swift",)))
    assert result["verdict"] == "solo"
    assert "unparsed languages in scope: swift" in result["reasons"]


def test_an_unresolved_graph_and_a_hub_in_a_cycle_stay_solo() -> None:
    edges = {**_pkg("pkga", 3, {"t1.py"}), **_pkg("pkgb", 3, {"t1.py"}), "t1.py": {"t2.py"}, "t2.py": {"t1.py"}}
    result = seam(_input(edges, unresolved_share=0.2))
    assert "a hub is inside a dependency cycle" in result["reasons"]
    assert any(reason.startswith("unresolved import share") for reason in result["reasons"])


def test_commons_are_left_out_of_the_graph() -> None:
    edges = {**_pkg("pkga", 3, {"shared/util.py"}), **_pkg("pkgb", 3, {"shared/util.py"}), "shared/util.py": set()}
    result = seam(_input(edges, commons=("shared/*",)))
    assert result["contracts"] == []
    assert [lane["name"] for lane in result["lanes"]] == ["pkga", "pkgb"]
    assert all(lane["depends_on"] == [] for lane in result["lanes"])


def test_lanes_max_merges_the_smallest_lanes() -> None:
    edges = {**_pkg("a", 3, {"b/m0.py"}), **_pkg("b", 3, set()), **_pkg("c", 3, set())}
    result = seam(_input(edges, lanes_max=1, min_lane=1))
    assert len(result["lanes"]) <= 2, "unconnected lanes can't merge, the connected pair can"


def _random_graph(rng: random.Random) -> dict[str, set[str]]:
    files = [f"p{rng.randrange(4)}/f{index}.py" for index in range(rng.randrange(4, 30))]
    return {path: {rng.choice(files) for _ in range(rng.randrange(3))} - {path} for path in files}


def _covers(glob: str, path: str) -> bool:
    return path == glob or (glob.endswith("/**") and path.startswith(glob[:-2]))


def _owners(result: dict, scope: frozenset[str], contracts: set[str]) -> dict[str, str]:
    """Each non-contract scope file's lane, failing if one is in two lanes."""
    owner: dict[str, str] = {}
    for lane in result["lanes"]:
        for path in sorted(scope - contracts):
            if any(_covers(glob, path) for glob in lane["globs"]):
                assert path not in owner, (path, owner.get(path), lane["name"])
                owner[path] = lane["name"]
    return owner


def _cross_edges(edges: dict[str, set[str]], owner: dict[str, str]) -> list[tuple[str, str]]:
    return [(s, t) for s, targets in edges.items() for t in targets if s in owner and t in owner and owner[s] != owner[t]]


def test_properties_on_random_graphs() -> None:
    rng = random.Random(7)
    for _ in range(200):
        edges = _random_graph(rng)
        seam_input = _input(edges)
        result = seam(seam_input)
        # Same input, same bytes.
        assert json.dumps(result, sort_keys=True) == json.dumps(seam(seam_input), sort_keys=True)
        contracts = {contract["path"] for contract in result["contracts"]}
        # No file in two lanes (`_owners`), and every cross-lane edge lands on a contract (so none is left between lanes).
        assert _cross_edges(edges, _owners(result, seam_input.scope, contracts)) == []


def test_a_go_package_is_one_unit() -> None:
    """Go files of one package import none of each other, yet every importer of the package imports them all."""
    event = {f"event/f{index}.go": set() for index in range(3)}
    every = set(event)
    edges = {
        **event,
        **{f"api/a{index}.go": set(every) for index in range(3)},
        **{f"web/w{index}.go": set(every) for index in range(3)},
    }
    result = seam(_input(edges))
    assert sorted(contract["path"] for contract in result["contracts"]) == sorted(event)
    assert [lane["name"] for lane in result["lanes"]] == ["api", "web"]
    assert result["verdict"] == "fan_out", result["reasons"]


def test_lanes_past_the_cap_join_the_nearest_folder() -> None:
    edges = {f"pkg{index}/sub/f{n}.py": set() for index in range(5) for n in range(3)}
    result = seam(_input(edges, lanes_max=2))
    assert len(result["lanes"]) <= 2


def test_a_lopsided_split_stays_solo() -> None:
    """One big lane and a small satellite is solo work, not a fan-out."""
    edges = {**_pkg("core", 20, set()), **_pkg("tiny", 3, set())}
    result = seam(_input(edges))
    assert result["verdict"] == "solo"
    assert any(reason.startswith("the largest lane holds 87%") for reason in result["reasons"]), result["reasons"]
