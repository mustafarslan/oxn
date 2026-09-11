"""The constraint bundle: ranked, capped, and honest about what it left out.

ADR-0006 section 4. The properties here are the three that make a *budget* different from a
truncated list: scope partitions the ranking, breadth decides when there is no scope to
partition by, and the cap never pretends the hidden constraints stopped applying.
"""

from __future__ import annotations

from pathlib import Path

from oxn.config import Config, Contract, Layer
from oxn.context.bundle import Project, build_bundle
from oxn.rules.adr import Decision

PATHS = ("src/api/one.py", "src/api/two.py", "src/core/three.py", "docs/note.md")


def _decision(identifier: str, scope: tuple[str, ...], **kwargs) -> Decision:
    return Decision(
        identifier=identifier,
        path=f"docs/adr/{identifier}.md",
        title=kwargs.pop("title", identifier),
        status=kwargs.pop("status", "accepted"),
        applies_to=scope,
        **kwargs,
    )


def _project(**kwargs) -> Project:
    config = Config(
        root=Path("."),
        ceilings=kwargs.pop("ceilings", {"cognitive_complexity": 12.0}),
        layers=kwargs.pop("layers", ()),
        contracts=kwargs.pop("contracts", ()),
        layer_ceilings=kwargs.pop("layer_ceilings", {}),
    )
    return Project(config=config, decisions=tuple(kwargs.pop("decisions", ())), paths=PATHS)


def _named(bundle) -> list[str]:
    return [constraint.name for constraint in bundle.constraints]


# ---- scope partitions, text orders within it --------------------------------------------


def test_a_constraint_covering_the_target_outranks_a_better_worded_one() -> None:
    """A task saying "fix the parser" cannot be expected to name the ceiling it is about,
    which is why scope decides and words only order what scope has already selected."""
    project = _project(
        decisions=[
            _decision("NARROW", ("src/api/*",), body="unrelated prose about licensing"),
            _decision("WIDE", ("docs/*",), body="retry timeout http client " * 20),
        ]
    )
    bundle = build_bundle(
        project, task="add a retry to the http client", targets=["src/api/one.py"]
    )
    names = _named(bundle)
    assert names.index("NARROW") < names.index("WIDE")


def test_within_the_same_scope_the_task_text_decides() -> None:
    project = _project(
        decisions=[
            _decision("QUIET", ("src/api/*",), body="unrelated prose about licensing"),
            _decision("LOUD", ("src/api/*",), body="retry timeout http client " * 20),
        ]
    )
    bundle = build_bundle(
        project, task="add a retry to the http client", targets=["src/api/one.py"]
    )
    names = _named(bundle)
    assert names.index("LOUD") < names.index("QUIET")


def test_with_no_target_files_breadth_decides() -> None:
    """Section 5a measured what text alone does with no scope to work from: chance. So the
    fallback is the one signal that beat it -- how much of the tree each constraint governs."""
    project = _project(
        decisions=[
            _decision("SMALL", ("src/core/*",)),
            _decision("BIG", ("src/*/*.py",)),
        ]
    )
    names = _named(build_bundle(project))
    assert names.index("BIG") < names.index("SMALL")


# ---- the cap is a display policy, never an enforcement one ------------------------------


def test_the_cap_holds_and_says_how_many_it_hid() -> None:
    project = _project(decisions=[_decision(f"ADR-{n}", ("src/*",)) for n in range(10)])
    bundle = build_bundle(project, limit=3)
    assert len(bundle.constraints) == 3
    assert bundle.omitted == 8  # ten decisions plus one project ceiling, minus the three shown


def test_the_bundle_says_the_gate_still_checks_what_it_did_not_show() -> None:
    """An agent inferring "seven constraints shown" means "seven constraints exist" has been
    misled by the budget rather than helped by it."""
    assert "shown or not" in build_bundle(_project(), limit=1).note


def test_nothing_is_omitted_when_everything_fits() -> None:
    assert build_bundle(_project()).omitted == 0


# ---- what an agent reads first ----------------------------------------------------------


def test_prose_and_enforced_rules_are_distinguishable() -> None:
    """A ceiling compiles to a rule the gate evaluates; a decision is prose it cannot check.
    Listing both without saying which is which is worse than showing one of them."""
    project = _project(decisions=[_decision("ADR-9", ("src/*",))])
    kinds = {c.name: c.enforced for c in build_bundle(project).constraints}
    assert kinds["cognitive_complexity"] is True
    assert kinds["ADR-9"] is False


def test_a_ceiling_an_adr_tightens_is_emitted_as_its_own_enforced_constraint() -> None:
    project = _project(decisions=[_decision("ADR-9", ("src/*",), ceilings={"file_sloc": 200.0})])
    shown = {c.name: c for c in build_bundle(project).constraints}
    assert shown["file_sloc (ADR-9)"].enforced is True
    assert shown["file_sloc (ADR-9)"].statement == "file_sloc <= 200"
    assert shown["ADR-9"].enforced is False


def test_a_decision_that_is_not_accepted_is_not_a_constraint() -> None:
    """ADR-0005: a proposed or superseded ADR is a document, not a rule."""
    project = _project(decisions=[_decision("DRAFT", ("src/*",), status="proposed")])
    assert "DRAFT" not in _named(build_bundle(project))


def test_a_contract_is_stated_in_one_line_with_its_layers_as_scope() -> None:
    project = _project(
        layers=(Layer("api", ("src/api/*",)), Layer("core", ("src/core/*",))),
        contracts=(Contract(name="layering", kind="layered", order=("api", "core")),),
    )
    (contract,) = [c for c in build_bundle(project).constraints if c.kind == "contract"]
    assert contract.statement == "layers api -> core: a layer may import only those after it"
    assert set(contract.scope) == {"src/api/*", "src/core/*"}
    assert contract.governs == 3


# ---- the emitted shape ------------------------------------------------------------------


def test_the_provenance_text_is_never_emitted() -> None:
    """It is the ADR's whole body -- the thing a bundle exists to avoid pasting at an agent."""
    project = _project(decisions=[_decision("ADR-9", ("src/*",), body="x" * 5000)])
    dumped = build_bundle(project).model_dump_json()
    assert "xxxx" not in dumped
    assert "text" not in build_bundle(project).constraints[0].model_dump()


def test_an_empty_project_produces_an_empty_bundle_rather_than_an_error() -> None:
    empty = Project(config=Config(root=Path(".")))
    bundle = build_bundle(empty, task="anything")
    assert bundle.constraints == () and bundle.omitted == 0


def test_with_targets_the_narrower_of_two_covering_constraints_comes_first() -> None:
    """Scope has already selected both, so specificity is what is left to say: a ceiling
    governing every file is ambient, one scoped to the area being edited exists because of
    it. This is the same `governs` number as the breadth prior, read the other way round."""
    project = _project(
        decisions=[
            _decision("EVERYWHERE", ("**",)),
            _decision("HERE", ("src/api/*",)),
        ]
    )
    names = _named(build_bundle(project, targets=["src/api/one.py"]))
    assert names.index("HERE") < names.index("EVERYWHERE")


# ---- the budget is shared, because BM25 scores are not comparable across shapes ---------


def test_prose_cannot_take_every_slot() -> None:
    """A six-token ceiling statement and a 17,000-character ADR are not comparable
    documents. Ranked in one list, prose wins everything: measured across the 53 labelled
    tasks before this split, 159 of 159 top-three slots were decisions -- at which point the
    bundle is "here are three documents to go read", which is what ADR-0006 section 4
    exists to reject."""
    project = _project(
        ceilings={"cognitive_complexity": 12.0, "file_sloc": 500.0, "a": 1.0, "b": 2.0},
        decisions=[_decision(f"ADR-{n}", ("src/*",), body="complexity " * 500) for n in range(9)],
    )
    shown = build_bundle(project, task="complexity", limit=6).constraints
    assert sum(1 for c in shown if c.enforced) == 3
    assert [c.enforced for c in shown] == [True, True, True, False, False, False]


def test_the_shorter_side_yields_its_slots_rather_than_wasting_them() -> None:
    """The bundle is only short when the project is."""
    one_rule = _project(decisions=[_decision(f"ADR-{n}", ("src/*",)) for n in range(9)])
    assert len(build_bundle(one_rule, limit=6).constraints) == 6

    no_decisions = _project(
        ceilings={f"rule_{n}": float(n) for n in range(9)},
        decisions=[],
    )
    assert len(build_bundle(no_decisions, limit=6).constraints) == 6


# ---- provenance: where a ceiling was declared -------------------------------------------


def test_a_declaration_that_does_not_cover_the_path_is_not_part_of_its_chain() -> None:
    """`declarations_of` answers "why is the ceiling here 8" and a layer the file is not in
    is not part of that answer, however tightly it constrains somewhere else."""
    from oxn.context.bundle import declarations_of

    project = _project(
        ceilings={"cognitive_complexity": 12.0},
        layers=(
            Layer(name="api", patterns=("src/api/*",)),
            Layer(name="core", patterns=("src/core/*",)),
        ),
        layer_ceilings={
            "api": {"cognitive_complexity": 8.0},
            "core": {"cognitive_complexity": 4.0},
        },
    )
    chain = declarations_of(project, "cognitive_complexity", "src/api/one.py")

    assert [(constraint.limit, constraint.name) for constraint in chain] == [
        (8.0, "cognitive_complexity in api"),
        (12.0, "cognitive_complexity"),
    ]


def test_the_chain_ignores_ceilings_for_other_rules() -> None:
    from oxn.context.bundle import declarations_of

    project = _project(ceilings={"cognitive_complexity": 12.0, "function_sloc": 60.0})
    chain = declarations_of(project, "function_sloc", "src/api/one.py")

    assert [constraint.limit for constraint in chain] == [60.0]
