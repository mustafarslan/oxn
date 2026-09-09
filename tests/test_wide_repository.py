"""Two classes of the same width, one legitimate, and what tells them apart.

`MAX_METHODS_PER_CLASS` was added to catch the God Class -- an accumulation of
responsibilities where every method is individually fine. It cannot do that on the count
alone, because a second shape sits at the same count and is not a defect: the **wide
repository**, one connection and one method per query. OXN's own `GraphStore` is one, at
NOM 26 and WMC 62 with no method above cyclomatic 6.

The fixtures here are that pair, held at the same NOM on purpose:

===============  =====================================================  ==========
fixture          what it is                                             components
===============  =====================================================  ==========
repository       15 queries over one `_conn`                            1
god_class        the same 15 methods over five field groups             5
===============  =====================================================  ==========

The five components the God Class breaks into *are* its five collaborators -- storage,
settings, cache, metrics, mail -- so the metric does not merely reject the class, it names
where to cut. That is the difference worth gating on, and it is why these ceilings are a
ratchet rather than a gate: the count is the trigger, and cohesion is the judgement.

**Constructors are excluded from the method graph, and this file is why.** A constructor
that assigns every field joins every component through it, so LCOM4 read `god_class` as a
single cohesive unit -- 1 component, the same answer it gives the repository, the false
negative exactly on the shape the metric exists to find.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cohesion_shapes"

#: Both fixtures sit here. Holding them equal is the point: a count cannot separate them.
SHARED_NOM = 15


def _model(fixture: str):
    """The `ClassModel` for the single class in one fixture."""
    from oxn.languages import get_parser
    from oxn.profiles import get_profile
    from oxn.resolve.members import build_class_models
    from oxn.resolve.scopes import build_scopes

    profile = get_profile("python")
    data = (FIXTURES / f"{fixture}.py.txt").read_bytes()
    tree = get_parser("python").parse(data)
    assert not tree.root_node.has_error, f"{fixture} does not parse"
    models = build_class_models(tree.root_node, profile, build_scopes(tree.root_node, profile))
    assert len(models) == 1, f"{fixture} should hold exactly one class"
    return next(iter(models.values()))


def _graph_store():
    """The real thing, measured through the same path `oxn classes` uses."""
    from oxn.resolve.project import class_models_for

    path = Path(__file__).resolve().parent.parent / "src" / "oxn" / "graph" / "store.py"
    parsed = class_models_for("src/oxn/graph/store.py", path)
    assert parsed is not None, "store.py failed to parse"
    return parsed[1]["GraphStore"]


@pytest.mark.parametrize("fixture", ("repository", "god_class"))
def test_the_count_cannot_separate_them(fixture: str) -> None:
    """The premise. If these ever drift apart the rest of the file proves nothing."""
    from oxn.thresholds import MAX_METHODS_PER_CLASS

    model = _model(fixture)
    assert len(model.methods) == SHARED_NOM, (
        f"{fixture} has {len(model.methods)} methods; the pair must stay matched"
    )
    assert SHARED_NOM > MAX_METHODS_PER_CLASS, (
        "the pair must sit above the ceiling, or neither shape is ever judged"
    )


def test_the_repository_is_one_thing_with_many_doors() -> None:
    """Every query goes through the one connection, so there is nothing to cut."""
    from oxn.metrics.cohesion import cohesion

    assert cohesion(_model("repository")).lcom4 == 1


def test_the_god_class_breaks_into_its_collaborators() -> None:
    """The finding: LCOM4 does not just reject it, it says where the seams are.

    Five field groups, five components. A constructor assigning all five fields used to
    bridge them into one -- the false negative this exclusion removes.
    """
    from oxn.metrics.cohesion import cohesion

    assert cohesion(_model("god_class")).lcom4 == 5


def test_graph_store_reads_as_a_repository_and_not_as_a_god_class() -> None:
    """OXN's own baselined class, and the reason it is not split.

    26 methods, all but one reaching `self._conn` -- directly, or through `_transaction`,
    which is what makes LCOM4 rather than LCOM3 the right reading. The odd one out is
    `__enter__`, a one-line `return self` that touches nothing and calls nothing.

    If this ever rises, `GraphStore` has grown a second responsibility and the split that
    was declined here is worth reopening.
    """
    from oxn.metrics.cohesion import cohesion

    model = _graph_store()
    measured = cohesion(model)
    assert measured.lcom3 >= 5, "LCOM3 alone would call this scattered; that is the point"
    assert measured.lcom4 <= 2, (
        f"GraphStore is now {measured.lcom4} components, not a wide repository"
    )


def test_lcom4_never_exceeds_lcom3_anywhere_in_oxn() -> None:
    """The invariant the shared exclusion protects, checked against real classes.

    LCOM4 is LCOM3 plus call edges, and an edge can only merge two components into one, so
    `lcom4 <= lcom3` holds by definition. It stops holding the moment a filter is applied to
    one variant and not the other -- which is exactly the mistake available when the next
    reader decides constructors should be dropped from LCOM4 alone.
    """
    from oxn.metrics.cohesion import cohesion
    from oxn.resolve.project import class_models_for

    root = Path(__file__).resolve().parent.parent / "src" / "oxn"
    checked = 0
    for path in sorted(root.rglob("*.py")):
        parsed = class_models_for(str(path), path)
        if parsed is None:
            continue
        for name, model in parsed[1].items():
            measured = cohesion(model)
            assert measured.lcom4 <= measured.lcom3, (
                f"{path.name}:{name} reads lcom4={measured.lcom4} > lcom3={measured.lcom3}"
            )
            checked += 1
    assert checked > 50, f"only {checked} classes reached; the sweep found nothing to check"
