"""`oxn.yaml` — the project's own statement of what it wants enforced.

Deliberately a **strict subset** of the schema P7 will need. Every key here is one P7 keeps
and extends; nothing here will have to be migrated when the rule engine lands. What P7 adds
is expressiveness — rules as predicates over the code graph, constraints compiled out of ADR
frontmatter — not a different shape for the same facts.

The file is optional. Without it OXN checks `src/oxn/thresholds.py`'s defaults and no
contracts, which is what a first `oxn check` in an unconfigured repository should do:
something useful, immediately, with nothing to read first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oxn import thresholds
from oxn.graph.contracts import Contract, Layer

#: The file OXN looks for, in the directory it is run from.
CONFIG_NAME = "oxn.yaml"

#: Where the ratchet lives. Inside `.oxn/` but deliberately *not* a cache: the baseline is
#: shared state that belongs in version control, and `.gitignore` un-ignores it explicitly.
BASELINE_NAME = Path(".oxn") / "baseline.json"

#: Every kind of entity that has a body a person reads top to bottom. The complexity and
#: length ceilings mean something here and nowhere else.
CALLABLE_KINDS: frozenset[str] = frozenset({"function", "method", "lambda"})
FILE_KINDS: frozenset[str] = frozenset({"file", "module"})


@dataclass(frozen=True, slots=True)
class Gate:
    """One gateable rule: which measurement, on what, against which default.

    A rule is not the same thing as a metric. `sloc` is measured on files, classes and
    functions alike, but "60 lines" is a statement about a *function*; applied to a module it
    flags every file in the project against a limit that was never about files. So the rule
    carries the kinds it applies to, and the two `sloc` ceilings are two rules.
    """

    metric: str
    threshold: str
    kinds: frozenset[str]


#: Rule name -> what it gates. Only these can appear under `ceilings:` in `oxn.yaml`;
#: anything else is a typo and is reported as one rather than silently ignored.
GATED_METRICS: dict[str, Gate] = {
    "cognitive_complexity": Gate(
        "cognitive_complexity", "MAX_COGNITIVE_COMPLEXITY", CALLABLE_KINDS
    ),
    "cyclomatic_complexity": Gate(
        "cyclomatic_complexity", "MAX_CYCLOMATIC_COMPLEXITY", CALLABLE_KINDS
    ),
    "max_nesting_depth": Gate("max_nesting_depth", "MAX_NESTING_DEPTH", CALLABLE_KINDS),
    "parameter_count": Gate("parameter_count", "MAX_PARAMETERS", CALLABLE_KINDS),
    "function_sloc": Gate("sloc", "MAX_FUNCTION_SLOC", CALLABLE_KINDS),
    "file_sloc": Gate("sloc", "MAX_FILE_SLOC", FILE_KINDS),
    # Anti-gaming, and deliberately sharing the cognitive ceiling rather than owning a
    # number of its own: the rule exists to stop that ceiling being satisfied by splitting
    # instead of simplifying, so the two must move together. Only cluster roots carry the
    # metric, so every other callable is simply absent from this check.
    "shredding": Gate("shredding_cluster", "MAX_COGNITIVE_COMPLEXITY", CALLABLE_KINDS),
}


class ConfigError(ValueError):
    """`oxn.yaml` says something OXN cannot act on. Never silently ignored."""


@dataclass(frozen=True, slots=True)
class Config:
    """Everything `oxn check` needs to know about this project's intent."""

    root: Path
    ceilings: dict[str, float] = field(default_factory=dict)
    #: Per-layer ceiling overrides: layer name -> {metric key: ceiling}. A generated or
    #: vendored layer usually wants a different bar from hand-written domain code.
    layer_ceilings: dict[str, dict[str, float]] = field(default_factory=dict)
    layers: tuple[Layer, ...] = ()
    contracts: tuple[Contract, ...] = ()
    exclude: tuple[str, ...] = ()
    #: Path globs whose findings are reported but never block. The escape hatch that keeps a
    #: gate from being switched off wholesale.
    advisory: tuple[str, ...] = ()

    @property
    def baseline_path(self) -> Path:
        return self.root / BASELINE_NAME

    def ceiling_for(self, metric: str, layer: str | None) -> float:
        """The ceiling that applies here: the layer's if it sets one, else the project's."""
        if layer is not None:
            override = self.layer_ceilings.get(layer, {}).get(metric)
            if override is not None:
                return override
        return self.ceilings[metric]

    def is_advisory(self, path: str) -> bool:
        from fnmatch import fnmatch

        return any(fnmatch(path, pattern) for pattern in self.advisory)

    @classmethod
    def defaults(cls, root: Path | None = None) -> Config:
        """What OXN enforces in a repository that has never configured it."""
        return cls(
            root=Path(root or Path.cwd()),
            ceilings={
                rule: float(getattr(thresholds, gate.threshold))
                for rule, gate in GATED_METRICS.items()
            },
        )

    @classmethod
    def load(cls, root: Path | None = None) -> Config:
        """Read `oxn.yaml` from `root`, falling back to the defaults when it is absent."""
        base = Path(root or Path.cwd())
        path = base / CONFIG_NAME
        if not path.is_file():
            return cls.defaults(base)
        import yaml  # lazily: ~15 ms, and an unconfigured project should not pay it

        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as error:  # pragma: no cover - message varies by yaml version
            raise ConfigError(f"{CONFIG_NAME} is not valid YAML: {error}") from error
        if not isinstance(raw, dict):
            raise ConfigError(f"{CONFIG_NAME} must be a mapping at the top level")
        return cls._from_mapping(base, raw)

    @classmethod
    def _from_mapping(cls, root: Path, raw: dict[str, Any]) -> Config:
        defaults = cls.defaults(root)
        ceilings = dict(defaults.ceilings)
        ceilings.update(_read_ceilings(raw.get("ceilings"), where="ceilings"))

        layers = tuple(
            Layer(name=name, patterns=tuple(_as_list(patterns, f"layers.{name}")))
            for name, patterns in _as_mapping(raw.get("layers"), "layers").items()
        )
        known = {layer.name for layer in layers}
        layer_ceilings = {
            name: _read_ceilings(values, where=f"layer_ceilings.{name}")
            for name, values in _as_mapping(raw.get("layer_ceilings"), "layer_ceilings").items()
        }
        for name in layer_ceilings:
            if name not in known:
                raise ConfigError(f"layer_ceilings names an undeclared layer: {name!r}")

        return cls(
            root=root,
            ceilings=ceilings,
            layer_ceilings=layer_ceilings,
            layers=layers,
            contracts=tuple(_read_contract(entry, known) for entry in _as_list_of_dicts(raw)),
            exclude=tuple(_as_list(raw.get("exclude"), "exclude")),
            advisory=tuple(_as_list(raw.get("advisory"), "advisory")),
        )


def _read_ceilings(raw: object, *, where: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for metric, ceiling in _as_mapping(raw, where).items():
        if metric not in GATED_METRICS:
            known = ", ".join(sorted(GATED_METRICS))
            raise ConfigError(f"{where}: {metric!r} is not a gated rule. Known rules: {known}")
        if not isinstance(ceiling, (int, float)) or isinstance(ceiling, bool):
            raise ConfigError(f"{where}.{metric} must be a number, got {ceiling!r}")
        values[metric] = float(ceiling)
    return values


def _read_contract(entry: dict[str, Any], layers: set[str]) -> Contract:
    name = entry.get("name")
    kind = entry.get("kind")
    if not isinstance(name, str) or not isinstance(kind, str):
        raise ConfigError(f"every contract needs a string `name` and `kind`: {entry!r}")
    order = tuple(_as_list(entry.get("order"), f"contracts.{name}.order"))
    for layer in order:
        if layer not in layers:
            raise ConfigError(f"contract {name!r} orders an undeclared layer: {layer!r}")
    return Contract(
        name=name,
        kind=kind,
        order=order,
        source=entry.get("source"),
        forbidden=tuple(_as_list(entry.get("forbidden"), f"contracts.{name}.forbidden")),
        modules=tuple(_as_list(entry.get("modules"), f"contracts.{name}.modules")),
        package=entry.get("package"),
        allowed_entrypoints=tuple(
            _as_list(entry.get("allowed_entrypoints"), f"contracts.{name}.allowed_entrypoints")
        ),
    )


def _as_mapping(raw: object, where: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping, got {type(raw).__name__}")
    return raw


def _as_list(raw: object, where: str) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ConfigError(f"{where} must be a string or a list of strings")
    return list(raw)


def _as_list_of_dicts(raw: dict[str, Any]) -> list[dict[str, Any]]:
    entries = raw.get("contracts")
    if entries is None:
        return []
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise ConfigError("contracts must be a list of mappings")
    return entries
