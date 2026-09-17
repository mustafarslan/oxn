"""The half of the repair harness that needs no model.

A sandbox, a measurement, and a verdict. Everything here is deterministic, which is the whole
argument for the harness's shape: a model is asked for an opinion only after the code has
already passed tests, lint, types, the ceiling and the shredding check -- so an opinion can
never rescue a candidate, only reject one.

Split out of `dogfood.py` because that file passed its own file ceiling and OXN said so. The
seam is real: nothing in this module knows that a language model exists.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from dogfood import Target

ROOT = Path(__file__).resolve().parent.parent

#: Where a sandbox is built. Outside the repository on purpose: a copy of the tree inside the
#: tree is a recursion waiting to happen, and a half-written sandbox must never be something
#: `oxn check` can see.
SCRATCH = Path(os.environ.get("OXN_SCRATCH", "/tmp")) / "oxn-dogfood"


#: Never copied into a sandbox. Basenames, plus paths relative to the repository root --
#: `shutil.ignore_patterns` matches basenames *only*, so the entry `benchmarks/corpora`
#: silently matched nothing and every sandbox copied 31 MB of other people's source. That
#: also broke linting: ruff honours `.gitignore` only inside a git repository, and a sandbox
#: has no `.git`, so it linted all of httpx and failed three otherwise-passing repairs.
SKIP_NAMES = frozenset(
    {
        ".git",
        ".venv",
        ".venvs",
        ".oxn",
        "tools",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
    }
)
SKIP_PATHS = frozenset({"benchmarks/corpora"})


def _skip(directory: str, names: list[str]) -> set[str]:
    here = Path(directory).resolve()
    ignored = {name for name in names if name in SKIP_NAMES or name.endswith(".pyc")}
    for relative in SKIP_PATHS:
        candidate = (ROOT / relative).resolve()
        if candidate.parent == here:
            ignored.add(candidate.name)
    return ignored


class SandboxNotReady(RuntimeError):
    """The bed cannot verify its own untouched code, so it cannot verify a repair.

    Raised rather than recorded, because there is no honest row to write: an arm scored
    against a sandbox whose checks were always going to fail is measuring the sandbox.
    """


class Sandbox:
    """A throwaway copy of a repository. The working tree is never touched.

    `root` is which repository. It defaulted to this one and was not a parameter at all,
    which was invisible while `select_targets` also hardcoded `src/oxn` and would have become
    a silent wrong answer the moment a second bed ran: targets measured in one tree, the
    repair verified in another. Unreachable today -- every bed but `self` is refused -- and
    "it can only fail to fire" is not a property worth relying on in a measurement.
    """

    def __init__(
        self,
        name: str,
        root: Path = ROOT,
        *,
        venv: bool = True,
        prepare: tuple[tuple[str, ...], ...] = (),
    ) -> None:
        # Unique per process. The path was `SCRATCH / target.leaf`, so two runs of the same
        # target shared one directory -- and `_repair_one` deletes its sandbox on exit, so
        # whichever finished first destroyed the other's tree mid-repair. It surfaced as
        # `FileNotFoundError` writing a candidate into a directory that had been removed,
        # in a pilot arm that had already spent five minutes on the attempt.
        #
        # An experiment runs the same target under six arms; running any two at once is the
        # obvious thing to do and would have collided every time.
        self.path = SCRATCH / f"{name}-{os.getpid()}"
        self.root = root
        self.venv = venv
        self.prepare = prepare
        self.python = self.path / ".venv" / "bin" / "python"

    def create(self) -> None:
        """Copy the tree, make the interpreter, and run what the *bed* says it needs.

        The install step used to be `uv pip install -e "{path}[dev]"` written in here --
        this project's own convention, applied to every bed. httpx has no `dev` extra, and
        `uv` does not refuse an extra that does not exist: it **exits 0 having installed
        nothing at all**, not even the package itself. So the sandbox had no `pytest`, every
        attempt was scored `tests FAIL`, and the arm table would have been a complete grid of
        zeros indistinguishable from a finding.

        `Bed.prepare` was declared for exactly this and read by nothing. It is read here now,
        and its commands must succeed -- a preparation step that fails silently is the same
        defect one layer up.
        """
        if self.path.exists():
            shutil.rmtree(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.root, self.path, ignore=_skip)
        if self.venv:
            # `--seed`, so the venv has `pip`: a bed's `prepare` is written as
            # `("-m", "pip", ...)` and routed through this interpreter, which is the only
            # spelling that works for every Python bed without any of them naming a path
            # that does not exist until the sandbox is made.
            subprocess.run(["uv", "venv", "-q", "--seed", str(self.path / ".venv")], check=True)
        for argv in self.prepare:
            finished = self.run(*argv, timeout=1800)
            if finished.returncode != 0:
                raise SandboxNotReady(
                    f"preparing {self.path.name}: `{' '.join(argv)}` exited "
                    f"{finished.returncode}\n{finished.stdout or finished.stderr}"
                )

    def run(self, *args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
        """One command in the sandbox.

        A leading `-m` means the bed's own interpreter, which is what a Python bed's checks
        are written against. Anything else runs as itself -- `go test`, `cargo clippy`,
        `npm test` -- because a Go module has no interpreter to route through and a harness
        that assumed one could only ever have had a single bed.
        """
        argv = [str(self.python), *args] if args and args[0] == "-m" else list(args)
        return subprocess.run(
            argv,
            cwd=self.path,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def destroy(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


# ---- the gauntlet -------------------------------------------------------------------------


#: Stands in for a score that could not be taken, and is deliberately enormous: every
#: comparison in the gauntlet treats lower as better, so an unmeasurable file must never
#: win one.
UNMEASURABLE = 999.0

#: The shape of a shred -- how many helpers, and how trivial -- is defined **once**, in
#: `oxn.thresholds`, and imported here. The harness used to own these numbers, which meant
#: the gate and the harness could disagree about what shredding is while both claimed to
#: detect it. The harness may be *stricter* than the gate (it can see the before-state, so
#: it catches marginal shreds the static rule cannot), but it must never be looser.
from oxn.thresholds import MANY_HELPERS, TRIVIAL_HELPER  # noqa: E402


@dataclass(frozen=True, slots=True)
class Measurement:
    """What one file's functions score, and what the target scores among them."""

    scores: dict[str, float]
    target: float
    present: bool
    #: Blocking `oxn check` findings for this file, as stable `rule|entity` keys. Compared
    #: before against after rather than run with `--no-baseline`, because a sandbox carries
    #: no `.oxn/` and the target file legitimately holds pre-existing debt -- the repair has
    #: to introduce nothing new, not fix everything that was already there.
    gate: frozenset[str] = frozenset()

    @property
    def mass(self) -> float:
        """Total complexity in the file. Reported, never gated -- see `GauntletResult`."""
        return sum(self.scores.values())

    @property
    def functions(self) -> int:
        return len(self.scores)


@dataclass
class GauntletResult:
    """Deterministic verification. Nothing subjective reaches this."""

    tests_pass: bool = False
    lint_pass: bool = False
    types_pass: bool = False
    #: Checks the bed does not declare. A Go module has no type checker to run, and a bed
    #: with no `types` entry must read as *not applicable* rather than as a failure -- while
    #: a declared check whose tool is missing stays a failure, because then the repair really
    #: was not verified. `skipped` is what keeps those two apart.
    skipped: set[str] = field(default_factory=set)
    #: Does `oxn check` -- the gate this project actually ships -- accept the repaired file?
    #: Defaults True so the many constructed results in tests stay about what they are about.
    gate_pass: bool = True
    score_before: float = 0.0
    score_after: float = 0.0
    file_mass_before: float = 0.0
    file_mass_after: float = 0.0
    functions_before: int = 0
    functions_after: int = 0
    #: Qualified name -> score, for every function that exists after the repair and did not
    #: exist before. The shredding test reads this and nothing else.
    new_helpers: dict[str, float] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    #: Does the function the actor was asked to simplify still exist? A deleted function
    #: is not a simplified one, and it used to score zero -- see `measure`.
    target_present: bool = True
    #: The ceiling the repair had to meet. Getting closer is not the same as arriving.
    ceiling: float = 0.0

    @property
    def toolchain_pass(self) -> bool:
        """Every check the bed declared, and none it did not.

        Three fields because the actor is told which one it broke; one question because a
        repair either survived the bed's toolchain or it did not.
        """
        return all(
            getattr(self, attribute) or label in self.skipped
            for label, attribute in _FIELDS.items()
        )

    @property
    def improved(self) -> bool:
        return self.score_after < self.score_before

    @property
    def under_ceiling(self) -> bool:
        """The target *and* every function the repair introduced.

        Checking only the target lets a repair "converge" by pushing twenty points into a
        new helper -- clearing the ceiling here by writing the next run's violation.
        """
        return self.score_after <= self.ceiling and all(
            score <= self.ceiling for score in self.new_helpers.values()
        )

    @property
    def shredded(self) -> bool:
        """Many new helpers, each of them trivial.

        docs/metrics.md §10.5 predicted the signature would be *total complexity mass
        staying put while function count jumps*, and this tested exactly that. A hand-built
        control falsified it: a 16-helper shred of `_imported_names` took the file from 137
        to **118**, while the cohesive three-helper extraction only reached 128 -- so the
        old rule rejected the good refactoring and accepted the shred.

        The cause is a property of the metric, not a badly chosen threshold. Fundamental
        increments survive extraction -- an `if` is still an `if` wherever it lives -- but
        **nesting increments evaporate, because every helper restarts at depth zero**. Mass
        therefore falls monotonically as a function is shredded harder, with a floor at the
        raw decision count. Mass cannot separate these two cases in any denominator, so it
        is reported and never gated.

        What does separate them is what the helpers are *worth*. A cohesive split yields
        helpers with bodies; a shred yields lines with names.
        """
        if len(self.new_helpers) < MANY_HELPERS:
            return False
        return median(self.new_helpers.values()) <= TRIVIAL_HELPER

    @property
    def passed(self) -> bool:
        """Every one of these is load-bearing, and two were added after a live run.

        `target_present` closes a soundness hole the harness had from the start: an actor
        that *deletes* the function scores zero on it, which `improved` reads as the best
        repair ever produced. Only the test suite caught it, and only because that function
        happened to be covered -- an uncovered one would have been accepted.

        `under_ceiling` is the difference between progress and success. A 35 taken to 14 is
        real work, but OXN would still block it, and a harness that accepts what the tool
        rejects is measuring the wrong thing.

        `shredded` is the deterministic half of the anti-gaming check. The other half is
        cohesion -- twelve helpers of three points each would clear every test here -- and
        cohesion is the judge's job. That makes judge/gauntlet agreement in `report` the
        calibration signal for this whole design rather than a decoration.

        A check the bed does not declare cannot fail it. `skipped` carries those, so a Go
        module with no type checker is not held to a `types_pass` nobody could satisfy --
        while a declared check whose tool is missing stays a failure, since the repair
        genuinely was not verified.
        """
        return (
            self.toolchain_pass
            and self.gate_pass
            and self.target_present
            and self.improved
            and self.under_ceiling
            and not self.shredded
        )


def verify_bed(sandbox: Sandbox, checks: tuple[tuple[str, ...], ...]) -> None:
    """Run the bed's own checks on its own untouched code, before any repair is scored.

    A bed that cannot verify its pristine tree cannot verify a repair, and the failure looks
    identical from the outside: `tests FAIL`, every attempt, every arm. This is the fifth
    defect of that family in this harness and the one with the largest blast radius --
    `uv pip install -e "{path}[dev]"` exits 0 having installed nothing on a project with no
    `dev` extra, so a full grid would have produced a complete table of zeros with nothing
    in it to say the sandbox was empty.

    A readiness *probe* -- import this, run that --  was the other option and is weaker. The
    baseline run proves the thing that matters, which is that these exact commands pass on
    this exact tree, and it needs no second mechanism to be kept in step with the first.

    Raises rather than returning a verdict: there is no honest row to write.
    """
    for label, *argv in checks:
        finished = sandbox.run(*argv)
        if finished.returncode != 0:
            raise SandboxNotReady(
                f"{sandbox.path.name}: `{' '.join(argv)}` ({label}) fails on the *unmodified* "
                f"tree, so no repair could ever pass it.\n"
                + (finished.stdout or finished.stderr)[-2000:]
            )


def run_gauntlet(
    sandbox: Sandbox,
    target: Target,
    before: Measurement,
    ceiling: int,
    checks: tuple[tuple[str, ...], ...],
) -> GauntletResult:
    """Verify a candidate deterministically, before any model is asked an opinion."""
    result = GauntletResult(
        score_before=before.target,
        file_mass_before=before.mass,
        functions_before=before.functions,
        ceiling=float(ceiling),
    )

    _run_toolchain(sandbox, result, checks)

    after = measure(sandbox, target)
    result.score_after = after.target
    result.file_mass_after = after.mass
    result.functions_after = after.functions
    result.new_helpers = {
        name: score for name, score in after.scores.items() if name not in before.scores
    }
    # The gate itself, applied as a ratchet. Without this the harness can accept what the
    # hook then rejects: a repair landing at 11 against a ceiling of 12 while adding three
    # dedicated helpers clears every check above and fails `oxn check` as shredding. This
    # makes "the harness is never looser than the gate" a property rather than a comment,
    # and any rule added to the gate later is enforced here without touching this file.
    introduced = sorted(after.gate - before.gate)
    result.gate_pass = not introduced
    if introduced:
        result.failures.append(
            "`oxn check` rejects this, and the hook will too:\n  " + "\n  ".join(introduced)
        )

    result.target_present = after.present
    if not result.target_present:
        result.failures.append(
            f"{target.leaf} no longer exists in {target.path}. The task is to simplify it, "
            f"not to remove it; every caller still expects it."
        )
    return result


def _run_toolchain(
    sandbox: Sandbox, result: GauntletResult, checks: tuple[tuple[str, ...], ...]
) -> None:
    """Every check the bed declares, filed under the label it declares them with.

    `checks` used to be this project's three commands written in. Both halves of ruff,
    because the project's own CI runs both: a harness weaker than CI accepts candidates that
    then fail it, which is how the first accepted repair came to need reformatting by hand
    before it would commit. Those are now `beds.SELF.verify`, and a Go bed's are `go test`
    and `go vet`.

    **Required, with no default.** It read `checks or SELF.verify`, so a caller that passed
    none silently verified one bed's repair with another bed's commands -- the same shape as
    `Sandbox.root` defaulting to this repository, which that class's docstring already names
    as the wrong-tree defect.

    **A check whose tool is missing is recorded as a failure, not a pass.** `cargo` absent
    from the machine means the repair was not verified, and a green row for a command that
    never ran is worse than a red one -- it is the same defect as measuring a stale cache.
    """
    _seed(result, checks)
    for label, *argv in checks:
        try:
            finished = sandbox.run(*argv)
        except FileNotFoundError:
            _record(result, label, passed=False, detail=f"{argv[0]} is not on PATH")
            continue
        _record(
            result,
            label,
            passed=finished.returncode == 0,
            detail=finished.stdout or finished.stderr,
        )


#: What each declared label sets. A label outside this is a bed asking for a check the
#: result has no field for, and is ignored rather than silently folded into another.
_FIELDS = {"tests": "tests_pass", "lint": "lint_pass", "types": "types_pass"}


def _seed(result: GauntletResult, declared: tuple[tuple[str, ...], ...]) -> None:
    """Every declared label starts True and is ANDed down; the rest are skipped.

    `lint` is two commands for this project, so a verdict has to be able to survive the
    first and fail on the second. And a bed that declares no `types` check gets `skipped`
    rather than a False that would read as a type error nobody found.
    """
    labels = {label for label, *_ in declared}
    for label, attribute in _FIELDS.items():
        if label in labels:
            setattr(result, attribute, True)
        else:
            result.skipped.add(label)


def _record(result: GauntletResult, label: str, *, passed: bool, detail: str) -> None:
    """One check's outcome, ANDed into its label's verdict."""
    attribute = _FIELDS.get(label)
    if attribute is None:
        return
    setattr(result, attribute, getattr(result, attribute) and passed)
    if not passed:
        result.failures.append(_tail(detail))


def measure(sandbox: Sandbox | None, target: Target) -> Measurement:
    """Per-entity cognitive complexity for the target's file.

    Per-entity rather than aggregate because the shredding test needs to know *which*
    functions are new and what each of them is worth -- a file total cannot answer either,
    and a file total was the whole of the evidence when the detector was inverted.

    **Measured with the harness's interpreter, not the bed's, and that distinction is the
    whole of why every external bed scored `999 -> 999`.** The bed's interpreter is for
    running the bed's checks: it lives in a venv holding the bed's dependencies, and `oxn` is
    not one of them, so `python -m oxn metrics` exited non-zero there and every score --
    including `score_before`, on the untouched file -- came back `UNMEASURABLE`. Nothing can
    improve on 999, so `improved` was always false, no candidate was ever judged, and the one
    bed where this worked was `self`, whose venv has `oxn` in it because `oxn` is what it is.
    Measuring is static analysis over source text; it needs a tree-sitter parse and no import
    of the code at all, so the bed's environment was never relevant to it.
    """
    base = sandbox.path if sandbox else ROOT
    interpreter = sys.executable
    result = subprocess.run(
        [interpreter, "-m", "oxn", "metrics", "--json", "--limit", "400", target.path],
        cwd=base,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # The file no longer parses. `UNMEASURABLE` keeps this out of every comparison that
        # treats a lower score as better.
        return Measurement(scores={}, target=UNMEASURABLE, present=False)
    rows = json.loads(result.stdout).get("entities", [])
    scores = {row["qualified_name"]: float(row["value"]) for row in rows}
    hits = [name for name in scores if name.endswith(f".{target.leaf}") or name == target.leaf]
    # A missing function is reported as missing, never as a score. Returning 0.0 here -- as
    # this did until a live run deleted `_imported_names` outright -- makes deletion look
    # like the strongest possible refactoring.
    return Measurement(
        scores=scores,
        target=scores[hits[0]] if hits else UNMEASURABLE,
        present=bool(hits),
        gate=_gate_findings(interpreter, base, target.path),
    )


def _gate_findings(interpreter: str, base: Path, path: str) -> frozenset[str]:
    """Blocking `oxn check` findings for one file, as `rule|entity` keys.

    Exit code is ignored on purpose: 2 means violations, which is the answer being asked
    for, and 1 means OXN itself failed, which yields no findings and is reported by the
    other checks rather than mistaken for a clean file.
    """
    result = subprocess.run(
        [interpreter, "-m", "oxn", "check", "--json", "--no-baseline", path],
        cwd=base,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        violations = json.loads(result.stdout)["violations"]
    except (json.JSONDecodeError, KeyError):
        return frozenset()
    return frozenset(
        f"{row['rule']}|{row['entity']}" for row in violations if row.get("blocking", True)
    )


def _tail(text: str, lines: int = 12) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])
