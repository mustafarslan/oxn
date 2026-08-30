"""Every tunable number in OXN, in one place, with how it was arrived at.

A threshold is a *parameter*, and a parameter that lives only as a literal in the file that
uses it cannot be fitted, compared across codebases, or argued with. This module makes the
whole parameter surface addressable as data: what the value is, where it came from, how many
observations back it, and therefore how much weight it deserves.

**On fitting these automatically.** The right answer is data, and OXN does not have enough
of it yet. `TRIVIAL_HELPER` is calibrated on **two** observations -- one cohesive extraction
and one hand-built shred -- and fitting a parameter to two examples labelled by the person
who chose the parameter is circular, whatever method is used on top. What the ceilings need
first is *observations*, which is why:

* every parameter here carries an explicit `Evidence` count and kind, so "calibrated on 2"
  is visible rather than implied;
* `benchmarks/dogfood-log.jsonl` records helper score distributions alongside both the
  deterministic verdict and the judge's, which makes each repair attempt a labelled example;
* P10 owns the fitting, against corpus percentiles for the ceilings and against accumulated
  labels for the anti-gaming parameters.

And it is worth being plain about the scale involved: fitting one or two scalars against a
few dozen labels is a grid search, not machine learning. Calling it the latter would dress a
lookup in borrowed authority. The corpus-percentile route (P10) needs no labels at all and
is the stronger method for the ceilings, because it answers "what is normal in real code"
rather than "what did we decide last Tuesday".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from oxn import thresholds


class Evidence(str, Enum):
    """How a value was arrived at, ordered from weakest to strongest.

    `(str, Enum)` rather than `StrEnum`: the latter is 3.11+ and OXN supports 3.10.
    """

    #: Chosen by judgement, with reasoning recorded and no measurement behind it.
    JUDGEMENT = "judgement"
    #: Taken from a published source -- a paper, a specification, a reference tool.
    LITERATURE = "literature"
    #: Fitted to observations OXN itself collected, with the count recorded.
    MEASURED = "measured"
    #: Derived from the distribution of a corpus, which is P10's target for the ceilings.
    CALIBRATED = "calibrated"


@dataclass(frozen=True, slots=True)
class Parameter:
    """One tunable number, and everything needed to judge how much to trust it."""

    name: str
    value: float
    evidence: Evidence
    #: How many observations back the value. Zero for judgement and literature values.
    observations: int
    #: Where the number came from, in one line.
    provenance: str
    #: What would have to exist before it could be fitted properly.
    fit_when: str = ""

    @property
    def is_provisional(self) -> bool:
        """True when the value would move given data OXN could plausibly collect."""
        return self.evidence is not Evidence.CALIBRATED

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "value": self.value,
            "evidence": self.evidence.value,
            "observations": self.observations,
            "provenance": self.provenance,
            "fit_when": self.fit_when,
            "provisional": self.is_provisional,
        }


#: Ceilings on a single function or file. Every one of these is a judgement or a citation,
#: never a measurement -- which is precisely what P10's corpus percentiles are for.
_CEILINGS: tuple[Parameter, ...] = (
    Parameter(
        name="MAX_COGNITIVE_COMPLEXITY",
        value=float(thresholds.MAX_COGNITIVE_COMPLEXITY),
        evidence=Evidence.JUDGEMENT,
        observations=0,
        provenance=(
            "12, between idea.md's proposed 8 and SonarSource's default 15. Neither endpoint "
            "is measured either: 15 is a product default, not a finding."
        ),
        fit_when="corpus percentiles exist for cognitive complexity per language (P10)",
    ),
    Parameter(
        name="MAX_CYCLOMATIC_COMPLEXITY",
        value=float(thresholds.MAX_CYCLOMATIC_COMPLEXITY),
        evidence=Evidence.LITERATURE,
        observations=0,
        provenance="McCabe (1976); NIST SP 500-235 discusses 10 and 15 as the usual band",
        fit_when="corpus percentiles exist (P10)",
    ),
    Parameter(
        name="MAX_PARAMETERS",
        value=float(thresholds.MAX_PARAMETERS),
        evidence=Evidence.LITERATURE,
        observations=0,
        provenance="Fowler, Refactoring (Long Parameter List); the 4-5 band from Clean Code",
    ),
    Parameter(
        name="MAX_NESTING_DEPTH",
        value=float(thresholds.MAX_NESTING_DEPTH),
        evidence=Evidence.JUDGEMENT,
        observations=0,
        provenance="conventional; nesting is what cognitive complexity already charges for",
        fit_when="corpus percentiles exist (P10)",
    ),
    Parameter(
        name="MAX_FUNCTION_SLOC",
        value=float(thresholds.MAX_FUNCTION_SLOC),
        evidence=Evidence.JUDGEMENT,
        observations=0,
        provenance="conventional rather than derived",
        fit_when="corpus percentiles exist (P10)",
    ),
    Parameter(
        name="MAX_FILE_SLOC",
        value=float(thresholds.MAX_FILE_SLOC),
        evidence=Evidence.JUDGEMENT,
        observations=0,
        provenance="conventional rather than derived",
        fit_when="corpus percentiles exist (P10)",
    ),
)

#: Parameters about *judgement* rather than size, and the only ones OXN has fitted to
#: anything it observed itself. Both need labels, not distributions: no percentile of a
#: corpus can say whether an extraction was cohesive.
_ANTI_GAMING: tuple[Parameter, ...] = (
    Parameter(
        name="DUPLICATION_MIN_TOKENS",
        value=float(thresholds.DUPLICATION_MIN_TOKENS),
        evidence=Evidence.MEASURED,
        observations=1,
        provenance=(
            "50 rather than PMD-CPD's 100: measured against PMD-CPD on the pinned corpus, "
            "where 100 missed the small copy-paste blocks agents actually produce"
        ),
        fit_when="clone recall is measured across more than one corpus",
    ),
    Parameter(
        name="TRIVIAL_HELPER",
        value=2.0,
        evidence=Evidence.MEASURED,
        observations=2,
        provenance=(
            "the median helper score separating one cohesive extraction (4, 9, 11) from one "
            "hand-built shred (16 helpers, median 1), both of `_imported_names`"
        ),
        fit_when=(
            "benchmarks/dogfood-log.jsonl holds ~50 labelled extractions; at that point this "
            "is a grid search over one scalar against judge and human labels"
        ),
    ),
    Parameter(
        name="MANY_HELPERS",
        value=3.0,
        evidence=Evidence.JUDGEMENT,
        observations=0,
        provenance=(
            "the smallest count that can mean 'many'; a three-way dispatch legitimately "
            "extracts three, so the count alone was never going to be the discriminator"
        ),
        fit_when="the same labelled set as TRIVIAL_HELPER",
    ),
)


def parameters() -> list[Parameter]:
    """The whole tunable surface. Adding a threshold anywhere means adding it here.

    Data at module level rather than inside a function body, because `oxn check` flagged the
    91-line function this used to be on the day it was written -- and a function whose entire
    body is a list is a list.
    """
    return [*_CEILINGS, *_ANTI_GAMING]


def summary() -> dict[str, object]:
    """The parameter surface as data, for a report or a future optimizer to consume."""
    values = parameters()
    return {
        "parameters": [parameter.as_dict() for parameter in values],
        "provisional": sum(1 for parameter in values if parameter.is_provisional),
        "total": len(values),
        "note": (
            "No parameter here is corpus-calibrated yet. P10 owns that work: percentiles "
            "for the ceilings, accumulated dogfood labels for the anti-gaming parameters."
        ),
    }
