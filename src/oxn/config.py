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

from collections.abc import Mapping
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
#: Entities that own methods. The class aggregates mean something here and nowhere else.
CLASS_KINDS: frozenset[str] = frozenset({"class", "interface"})


def _find_project(start: Path) -> Path:
    """The nearest directory at or above `start` that configures OXN.

    Bounded by the repository, not by the filesystem: ascending past a directory holding
    `.git` would let someone's home-directory `oxn.yaml` govern an unrelated checkout. With
    neither found, the answer is where we started, which is what an unconfigured project
    wants anyway.
    """
    current = start.resolve()
    for directory in (current, *current.parents):
        if (directory / CONFIG_NAME).is_file():
            return directory
        if (directory / ".git").exists():
            break
    return current


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
    #: Another rule whose *configured* ceiling this one follows. An anti-gaming rule has no
    #: number of its own: it exists to stop one ceiling being satisfied dishonestly, so it
    #: has to move when a project moves that ceiling. Sharing only the default is not
    #: enough -- a project that raises `cognitive_complexity` to 20 would otherwise keep
    #: being told 14 is shredding while a plain 19-point function passes, which inverts the
    #: rule's own premise.
    follows: str = ""


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
    # The class aggregates, and the reason they are here rather than only in `oxn classes`:
    # `shredding` clusters a function with the *private* helpers *only it calls*, and both
    # premises are evadable -- give the helpers public names, or a second call site, and the
    # cluster disappears while the work stays spread. Measured in
    # `tests/test_class_scope_evasion.py`: two of the three evasions walk through the gate,
    # and both aggregates separate them from a legitimate decomposition by a wide margin.
    # Exact and file-local, so they may block under ADR-0002.
    "methods_per_class": Gate("nom", "MAX_METHODS_PER_CLASS", CLASS_KINDS),
    "weighted_methods_per_class": Gate("wmc", "MAX_WEIGHTED_METHODS_PER_CLASS", CLASS_KINDS),
    # Anti-gaming, and deliberately sharing the cognitive ceiling rather than owning a
    # number of its own: the rule exists to stop that ceiling being satisfied by splitting
    # instead of simplifying, so the two must move together. Only cluster roots carry the
    # metric, so every other callable is simply absent from this check.
    "shredding": Gate(
        "shredding_cluster",
        "MAX_COGNITIVE_COMPLEXITY",
        CALLABLE_KINDS,
        follows="cognitive_complexity",
    ),
}

#: The contract kinds `contracts:` accepts. Declared here rather than only in
#: `rules.builtin.contract_rules`, which *skips* what it does not recognise -- so a
#: misspelled kind built no rule, fired never, and reported nothing.
CONTRACT_KINDS = frozenset({"layered", "forbidden", "independence", "deep_import", "acyclic"})


class ConfigError(ValueError):
    """`oxn.yaml` says something OXN cannot act on. Never silently ignored."""


@dataclass(frozen=True, slots=True)
class Config:
    """Everything `oxn check` needs to know about this project's intent."""

    root: Path
    #: Rule name -> ceiling, for the rules this project gates on. A rule switched off is
    #: **absent from this map**, which is what disables it: `rules.facts._ceilings` emits one
    #: `ceiling` row per entry, every ceiling rule joins on that row, and a rule with no row
    #: cannot fire. Off is therefore the absence of a limit rather than an infinite one, and
    #: nothing downstream needs to learn a second way to say no.
    ceilings: dict[str, float] = field(default_factory=dict)
    #: Rules the project turned off, kept for reporting. Not consulted by the gate.
    disabled: frozenset[str] = frozenset()
    #: Per-layer ceiling overrides: layer name -> {metric key: ceiling}. A generated or
    #: vendored layer usually wants a different bar from hand-written domain code.
    layer_ceilings: dict[str, dict[str, float]] = field(default_factory=dict)
    layers: tuple[Layer, ...] = ()
    contracts: tuple[Contract, ...] = ()
    #: Path globs this project declares out of scope: generated code, vendored trees,
    #: fetched corpora. Matched against the repo-relative path, and applied wherever files
    #: are found -- the walk, and a file named directly on the command line -- because a
    #: hook that names the file the agent just edited would otherwise gate exactly the
    #: generated code this key exists to exempt. Distinct from `advisory`, which reports a
    #: finding without blocking; `exclude` means the file is never looked at.
    exclude: tuple[str, ...] = ()
    #: Path globs whose findings are reported but never block. The escape hatch that keeps a
    #: gate from being switched off wholesale.
    advisory: tuple[str, ...] = ()
    #: How many repairs an agent may spend on one violation before OXN stops asking and
    #: reports instead (ADR-0003 section 4). `0` disables the budget: the gate keeps saying
    #: no for as long as the violation is there, which is the correct behaviour in CI and
    #: the wrong one in front of an agent that cannot converge.
    retry_budget: int = thresholds.RETRY_BUDGET

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
        """Read `oxn.yaml`, falling back to the defaults when there is none.

        With no `root`, the project is *found* rather than assumed: `oxn.yaml` lives at the
        top of a repository and commands get run from inside it. Assuming the working
        directory was the project root meant that `cd src && oxn check --deep` reported "no
        contracts declared in oxn.yaml" against a repository whose `oxn.yaml` declares
        several -- the gate quietly weakening to defaults, and the baseline beside it going
        unread, because both are paths relative to a root that was two levels up.
        """
        base = Path(root) if root is not None else _find_project(Path.cwd())
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
        ceilings, disabled = _project_ceilings(cls.defaults(root), raw.get("ceilings"))

        layers = tuple(
            Layer(name=name, patterns=tuple(_as_list(patterns, f"layers.{name}")))
            for name, patterns in _as_mapping(raw.get("layers"), "layers").items()
        )
        known = {layer.name for layer in layers}
        layer_ceilings = {
            name: _numbers(_read_ceilings(values, where=f"layer_ceilings.{name}"))
            for name, values in _as_mapping(raw.get("layer_ceilings"), "layer_ceilings").items()
        }
        for name in layer_ceilings:
            if name not in known:
                raise ConfigError(f"layer_ceilings names an undeclared layer: {name!r}")

        return cls(
            root=root,
            ceilings=ceilings,
            disabled=disabled,
            layer_ceilings=layer_ceilings,
            layers=layers,
            contracts=tuple(_read_contract(entry, known) for entry in _as_list_of_dicts(raw)),
            exclude=tuple(_as_list(raw.get("exclude"), "exclude")),
            advisory=tuple(_as_list(raw.get("advisory"), "advisory")),
            retry_budget=_read_retry_budget(raw.get("retry_budget")),
        )


def _read_retry_budget(raw: object) -> int:
    """A whole number of attempts, or the default. Rejected loudly rather than coerced.

    `True` is an `int` in Python and would arrive here as a budget of one, which is a
    silently working configuration that means nothing anybody wrote it to mean.
    """
    if raw is None:
        return thresholds.RETRY_BUDGET
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise ConfigError(f"retry_budget must be a non-negative whole number, got {raw!r}")
    return raw


def _project_ceilings(
    defaults: Config, raw: object
) -> tuple[dict[str, float], frozenset[str]]:
    """The ceilings in force, and the rules this project switched off.

    A rule turned off is *removed* rather than set to infinity: every ceiling rule joins on
    a `ceiling` row, so an absent row is already the way to say "this does not apply", and
    inventing a second one would give the engine two spellings of the same decision.
    """
    declared = _read_ceilings(raw, where="ceilings")
    disabled = _switched_off(declared)
    ceilings = {**defaults.ceilings, **_numbers(declared)}
    return {rule: limit for rule, limit in ceilings.items() if rule not in disabled}, disabled


def _numbers(declared: Mapping[str, float | None]) -> dict[str, float]:
    """The entries that named a limit, dropping the ones that said `off`."""
    return {rule: limit for rule, limit in declared.items() if limit is not None}


def _switched_off(declared: Mapping[str, float | None]) -> frozenset[str]:
    """The rules a project turned off, plus the anti-gaming rules that only exist for them.

    `shredding` has no ceiling of its own: it *follows* `cognitive_complexity`, because it
    exists to stop that one ceiling being met by splitting instead of simplifying. With the
    ceiling gone there is nothing for it to protect, and leaving it on would keep rejecting
    a shape whose honest version now passes.
    """
    off = {rule for rule, limit in declared.items() if limit is None}
    return frozenset(
        off | {rule for rule, gate in GATED_METRICS.items() if gate.follows in off}
    )


#: What a project writes to switch a rule off. `off` and `false` both, because YAML reads a
#: bare `off` as the boolean False and a quoted `"off"` as the string, and a config file that
#: behaves differently depending on which one was typed is a trap rather than a feature.
_OFF = frozenset({"off", "false", "none", "disabled"})


def _read_ceilings(raw: object, *, where: str) -> dict[str, float | None]:
    """Rule -> ceiling, with `None` for a rule the project turned off.

    A number tightens or loosens a gate; `off` removes it. Both are declarations a project
    makes on purpose, which is why the second is a value here rather than a separate key: a
    reader of `oxn.yaml` sees every rule in one list and what this project decided about it.
    """
    values: dict[str, float | None] = {}
    for metric, ceiling in _as_mapping(raw, where).items():
        if metric not in GATED_METRICS:
            known = ", ".join(sorted(GATED_METRICS))
            raise ConfigError(f"{where}: {metric!r} is not a gated rule. Known rules: {known}")
        values[metric] = _one_ceiling(ceiling, where=f"{where}.{metric}")
    return values


def _one_ceiling(ceiling: object, *, where: str) -> float | None:
    """A number, or `None` for one of the spellings of off."""
    if ceiling is False or (isinstance(ceiling, str) and ceiling.strip().lower() in _OFF):
        return None
    if not isinstance(ceiling, (int, float)) or isinstance(ceiling, bool):
        raise ConfigError(f"{where} must be a number or `off`, got {ceiling!r}")
    return float(ceiling)


def _read_contract(entry: dict[str, Any], layers: set[str]) -> Contract:
    name = entry.get("name")
    kind = entry.get("kind")
    if not isinstance(name, str) or not isinstance(kind, str):
        raise ConfigError(f"every contract needs a string `name` and `kind`: {entry!r}")
    if kind not in CONTRACT_KINDS:
        # A misspelled kind used to be silently inert: `contract_rules` skips what it does
        # not recognise, so `kind: acylic` declared a contract that could never fire and
        # said nothing. A gate that quietly does not run is worse than one that is off.
        known = ", ".join(sorted(CONTRACT_KINDS))
        raise ConfigError(f"contract {name!r}: {kind!r} is not a contract kind. Known: {known}")
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
        deferred=bool(entry.get("deferred", False)),
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
