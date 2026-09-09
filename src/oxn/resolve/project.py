"""Class member models for a whole project, stitched from the per-file ones.

`resolve.members` answers "what does this class, written here, touch". That is the right
question one file at a time and the wrong one for two of the six languages: Go declares a
method at file scope carrying a receiver, and Rust puts a type's methods in `impl` blocks, so
either may spread one type across a package. Asked per file, `Counter` came back twice --
two methods beside the struct and three in its sibling -- and neither answer was the type.

This module is the join, and it is the reading path's half of what
`indexer.aggregate_classes` does for the gate. The two must agree: a type with two method
counts has one that a ceiling gets compared against, and it is the smaller.

Analysis rather than a surface, for the reason `resolve.symbols` is: it reads the graph and
the profiles, holds no presentation, and the surfaces read it. It moved here from `report.py`
when that file hit its own `file_sloc` ceiling -- a project-level model builder was never a
reporting concern.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ClassViews:
    """The per-file views the CK suite needs, keyed alike.

    One object rather than four dicts passed around together: they share a key space, are
    always built in one pass, and are always read as a group.
    """

    models: dict[str, Any] = dc_field(default_factory=dict)
    entities: dict[str, Any] = dc_field(default_factory=dict)
    scopes: dict[str, Any] = dc_field(default_factory=dict)
    weights: dict[str, Any] = dc_field(default_factory=dict)


def build_class_views(indexer: Any, files: Any) -> ClassViews:
    """Parse and measure every readable file into the views above."""
    views = ClassViews()
    for path in files:
        parsed = class_models_for(indexer.relative(path), path)
        if parsed is None:
            continue
        relative, models, entities, scopes, weights = parsed
        views.models[relative] = models
        views.entities[relative] = entities
        views.scopes[relative] = scopes
        views.weights[relative] = weights
    _merge_split_classes(views)
    return views


def _merge_split_classes(views: ClassViews) -> None:
    """Fold a type declared in one file and extended in another into one model.

    Go declares a method at file scope carrying a receiver, and Rust puts a type's methods in
    `impl` blocks: either language may spread one type across a package's files. Measured per
    file, `Counter` came back *twice* -- two methods beside the struct and three in its
    sibling -- and neither row was the type. Worse than a split count: the sibling's methods
    read fields its own tree never saw declared, so the two halves looked like two types that
    share nothing, and LCOM4 read 3 where the type has 1.

    The gate has joined at package scope since `indexer.aggregate_classes`. This is the same
    join for the reading path, and `tests/test_cross_language_cohesion.py` holds the two
    equal -- a type with two method counts has one that a ceiling gets compared against.

    A package is a directory, which is Go's own rule and therefore exact. Rust's unit is a
    module, so a type whose `impl` sits in another directory is still missed; that is a lower
    bound, and the same one the gate has.
    """
    from oxn.profiles import profile_for_path
    from oxn.resolve.members import settle_fields

    grouped: dict[tuple[str, str], list[str]] = {}
    for relative, models in views.models.items():
        profile = profile_for_path(relative)
        if profile is None or not (profile.receiver_field or profile.implements_field):
            continue
        for name in models:
            grouped.setdefault((relative.rpartition("/")[0], name), []).append(relative)

    for (_directory, name), paths in grouped.items():
        if len(paths) < 2:
            continue
        home = _home_of(name, paths, views)
        target = views.models[home][name]
        for other in sorted(set(paths) - {home}):
            _absorb(target, views.models[other].pop(name))
            views.weights[home].setdefault(name, {}).update(views.weights[other].get(name, {}))
        settle_fields(target)


def _home_of(name: str, paths: list[str], views: ClassViews) -> str:
    """The file a merged type is reported against: the one that declares it.

    A Go method lives beside its struct or in a sibling, and only the struct's file has the
    fields. Reporting the type against a file holding three of its methods and none of its
    state is an answer a reader cannot act on.
    """
    ordered = sorted(paths)
    declaring = [path for path in ordered if views.models[path][name].fields]
    return declaring[0] if declaring else ordered[0]


def _absorb(target: Any, extra: Any) -> None:
    """Everything the other file knew about this type, added to the model that keeps it."""
    target.fields |= extra.fields
    target.methods.update(extra.methods)
    target.properties |= extra.properties
    target.bases = target.bases + tuple(base for base in extra.bases if base not in target.bases)


def class_models_for(relative: str, path: Path) -> tuple[str, Any, Any, Any, Any] | None:
    """Parse one file into the views the CK suite needs, or `None` if it cannot be read.

    A file that fails to parse is skipped rather than guessed at: half a syntax tree yields a
    cohesion number that looks like a measurement and is not one.

    The file is also *measured* here, for one reason: WMC is a weighted sum and the weights
    are per-method complexities. Without them `ck_metrics` weighted every method 1 and WMC
    was NOM under another name -- see `coupling.method_weights`.
    """
    from oxn.graph.builder import build_file
    from oxn.languages import get_parser
    from oxn.metrics.coupling import method_weights
    from oxn.metrics.engine import measure_file
    from oxn.profiles import profile_for_path
    from oxn.resolve.members import build_class_models
    from oxn.resolve.scopes import build_scopes

    profile = profile_for_path(str(path))
    if profile is None:
        return None
    source = path.read_bytes()
    tree = get_parser(profile.name).parse(source)
    if tree.root_node.has_error:
        return None
    scopes = build_scopes(tree.root_node, profile)
    entities = list(build_file(relative, source, profile, tree.root_node).entities)
    measured = measure_file(entities, source, profile, tree.root_node)
    return (
        relative,
        build_class_models(tree.root_node, profile, scopes),
        entities,
        scopes,
        method_weights(measured),
    )
