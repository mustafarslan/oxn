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
lookup in borrowed authority.

**The corpus-percentile route was measured. Which percentile matters more than it sounds.**

*Unweighted*, over entities, it is unusable. These distributions have median 0: 69.4% of
nest's callables are anonymous arrow functions and 92.7% of those score zero cognitive
complexity, so the 95th percentile is **2 in TypeScript and 9 in Go**. A ceiling fitted
there would reject most ordinary functions in either language.

*LOC-weighted*, which is what `docs/metrics.md` section 10.3 Mode C actually cites -- Alves,
Ypma & Visser, ICSM 2010, weighting each entity by its own length so a 500-line function
counts 500 times a one-line one -- it is sound, and it **corroborates the ceilings already
here**. `MAX_COGNITIVE_COMPLEXITY = 12` sits at P79 (go) to P98 (java) of the weighted mass,
inside the p80/p90 band that paper proposes as its high-risk boundary; cyclomatic 10 at
P86-P99, nesting 4 at P96-P100. This module claimed the percentile route was "the stronger
method" before any of it was measured, and the first version of this amendment then
overcorrected into refusing percentiles outright. Neither was right: the method is fine, the
aggregation is what the trivial mass breaks.

**They are still not fitted to it,** and that is a governance decision rather than a
statistical one. A fitted ceiling is a function of which repositories were benchmarked --
the weighted p90 of cognitive complexity ranges 7 to 23 across these five -- so it would
move whenever a corpus was re-pinned or a sixth language added. That is
`freeze_retrieval_corpus.py`'s seventeen re-pins with a gate behind them.

What the corpora give the record below is **exceedance**: the fraction of real code a
ceiling rejects, which is comparable across languages without an aggregation choice standing
between the number and its meaning. It moved no value, and saying so is the point -- the
ceilings are still judgements, now judgements with a measured cost and an independent
method agreeing with them.
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
    #: Fitted to the distribution of a corpus. No parameter is, and the ceilings are not
    #: going to be: see the module docstring -- a fitted ceiling tracks the corpus set.
    CALIBRATED = "calibrated"


@dataclass(frozen=True, slots=True)
class Parameter:
    """One tunable number, and everything needed to judge how much to trust it."""

    name: str
    value: float
    evidence: Evidence
    #: Observations of this parameter's *cost at its current value*, in whatever unit
    #: `fit_when` names -- **not** how many it was fitted to. `evidence` already carries that
    #: distinction, and conflating the two made P10's exit criterion ("no gated threshold
    #: still labelled `judgement` with zero observations") unsatisfiable except by fitting: a
    #: ceiling could not be *measured* without also being *changed*. It can. A judgement with
    #: 10,256 observations behind its cost is still a judgement, and `is_provisional` says so.
    #:
    #: Still zero where nobody has measured: `BM25_K1` and `BM25_B` have 153 labelled pairs
    #: (P8) evaluated at their current values and are the obvious next entries to fill.
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


#: Ceilings on a single function or file. Every one is still a judgement or a citation: the
#: P10 measurement told us what each one *costs*, not what it should be.
#:
#: `observations` is the **named** callable population across the five `use: threshold`
#: corpora of `benchmarks/manifest.yaml` -- httpx, go-kit, ripgrep, petclinic, nest -- which
#: is 10,256 functions and methods, or 2,389 files for `MAX_FILE_SLOC`. Anonymous callables
#: are excluded from the comparison because their density is not comparable (0.1% of httpx's
#: callables, 69.4% of nest's); `benchmarks/ceiling-observations.json` carries both rows, and
#: they disagree where it matters: TypeScript's `function_sloc` exceedance is 3.16% over all
#: callables and 0.67% over named ones, so nest's long callables are inline callbacks.
_CEILINGS: tuple[Parameter, ...] = (
    Parameter(
        name="MAX_COGNITIVE_COMPLEXITY",
        value=float(thresholds.MAX_COGNITIVE_COMPLEXITY),
        evidence=Evidence.JUDGEMENT,
        observations=10256,
        provenance=(
            "12, between idea.md's proposed 8 and SonarSource's default 15. Neither endpoint "
            "is measured either: 15 is a product default, not a finding. Measured cost: it "
            "rejects 0.49% (java) to 3.32% (go) of named callables across the five corpora."
        ),
        fit_when=(
            "not an unweighted percentile -- p95 of these distributions is 2 in TypeScript and 9 "
            "in Go. LOC-weighted (Alves et al.) it already sits at P79-P98, so the "
            "corpora corroborate 12 rather than propose a replacement for it. "
            "What would move it is a labelled set of functions where the gate was wrong, "
            "which `benchmarks/dogfood-log.jsonl` collects one repair at a time"
        ),
    ),
    Parameter(
        name="MAX_CYCLOMATIC_COMPLEXITY",
        value=float(thresholds.MAX_CYCLOMATIC_COMPLEXITY),
        evidence=Evidence.LITERATURE,
        observations=10256,
        provenance=(
            "McCabe (1976); NIST SP 500-235 discusses 10 and 15 as the usual band. Measured "
            "cost: rejects 0.48% (typescript) to 2.03% (python) of named callables."
        ),
        fit_when="a published threshold with a corpus behind it, rather than a band",
    ),
    Parameter(
        name="MAX_PARAMETERS",
        value=float(thresholds.MAX_PARAMETERS),
        evidence=Evidence.LITERATURE,
        observations=10256,
        provenance=(
            "Fowler, Refactoring (Long Parameter List); the 4-5 band from Clean Code. "
            "Measured cost: rejects 0.00% (java) to 3.62% (python) of named callables -- the "
            "widest language spread of the callable ceilings, and Python owns the top of it."
        ),
        fit_when="a per-language ceiling, if Python's keyword-argument idiom warrants one",
    ),
    Parameter(
        name="MAX_NESTING_DEPTH",
        value=float(thresholds.MAX_NESTING_DEPTH),
        evidence=Evidence.JUDGEMENT,
        observations=10256,
        provenance=(
            "conventional; nesting is what cognitive complexity already charges for. Measured "
            "cost: rejects 0.00% (go) to 0.96% (rust) of named callables -- the least "
            "load-bearing ceiling here, which is consistent with it being double-charged."
        ),
        fit_when="evidence it catches anything `cognitive_complexity` does not catch first",
    ),
    Parameter(
        name="MAX_FUNCTION_SLOC",
        value=float(thresholds.MAX_FUNCTION_SLOC),
        evidence=Evidence.JUDGEMENT,
        observations=10256,
        provenance=(
            "conventional rather than derived. Measured cost: rejects 0.49% (java) to 2.70% "
            "(go) of named callables."
        ),
        fit_when="the same labelled set as the cognitive ceiling",
    ),
    Parameter(
        name="MAX_FILE_SLOC",
        value=float(thresholds.MAX_FILE_SLOC),
        evidence=Evidence.JUDGEMENT,
        observations=2389,
        provenance=(
            "conventional rather than derived, and the one ceiling the measurement argues "
            "with. Measured cost across 2,389 files: 0.00% (java), 0.39% (go), 1.57% "
            "(typescript), 10.00% (python), **20.91% (rust)** -- a 50x spread where every "
            "other ceiling holds inside 7x. The Rust files above it are hand-written core, "
            "not generated: ripgrep's flag table (6,916), its printer (3,220), its directory "
            "walker (1,899). Held at 500 anyway, because one repository per language cannot "
            "distinguish 'Rust is written this way' from 'ripgrep is written this way', and "
            "because `oxn.yaml` already lets a project raise it without moving the default."
        ),
        fit_when=(
            "a second Rust and a second Python repository. If the spread survives them it is "
            "a language property, and this is the ceiling that should become per-language"
        ),
    ),
    Parameter(
        name="MAX_METHODS_PER_CLASS",
        value=float(thresholds.MAX_METHODS_PER_CLASS),
        evidence=Evidence.JUDGEMENT,
        observations=3847,
        provenance=(
            "12, a round number inside a window a control defines rather than a percentile. "
            "The two shred shapes `shredding` cannot see -- public helper names, and helpers "
            "given a second caller -- score NOM 14, while the legitimate decomposition of the "
            "same work scores 4, so anything in 5..13 separates them. Measured across 3,847 "
            "classes: rejects 0.00% (go-kit, whose largest type has 9 methods) to 7.48% "
            "(httpx), and 0.7% of OXN's own 149. "
            "Higher than the per-function ceilings because the evasions sit inside the "
            "legitimate distribution rather than beyond it, which is why this is a ratchet "
            "first. **The population is every class entity, not the named ones**, because "
            "the unnamed ones are where the methods are: a Rust `impl` block is an unnamed "
            "class holding its type's methods, and ripgrep reads 840 class entities against "
            "395 named. **Go needs a receiver join**, since a method there is a top-level "
            "declaration rather than a member of its type: without it every struct measured "
            "NOM 0 and the ceiling was silently inert for the language. The join is by "
            "package, which Go's own rule makes exact -- see `indexer.aggregate_classes`."
        ),
        fit_when=(
            "a second evasion pair. Both this and the WMC ceiling are positioned by a single "
            "authored control, which is why neither sits at its window's edge. The other "
            "half of the evidence is not authored: across the five corpora the two ceilings "
            "reject 115 classes, 28 of them by NOM alone and 23 by WMC alone, so neither is "
            "a restatement of the other. Seven of the 115 are test classes, which is the "
            "known false positive -- many small test methods is a legitimate shape, and "
            "`oxn.yaml`'s advisory paths are the answer rather than a looser ceiling"
        ),
    ),
    Parameter(
        name="MAX_WEIGHTED_METHODS_PER_CLASS",
        value=float(thresholds.MAX_WEIGHTED_METHODS_PER_CLASS),
        evidence=Evidence.JUDGEMENT,
        observations=3847,
        provenance=(
            "25, positioned like its sibling: the escapes score WMC 27 and the legitimate "
            "decomposition 17, so 18..26 separates them. Weighted by *cyclomatic* complexity "
            "per docs/metrics.md section 3.6 and for a measured reason -- cognitive weights "
            "fall under extraction, so a cognitive WMC would report an improvement exactly "
            "when work is being spread. Measured cost: 0.27% (go-kit) to 10.28% (httpx), "
            "and 2.0% of OXN's own classes."
        ),
        fit_when=(
            "the same second evasion pair as MAX_METHODS_PER_CLASS. What already argues for "
            "this one independently: of the 87 classes it rejects across the corpora, 56 "
            "contain no method over the *per-function* cyclomatic ceiling -- every method "
            "individually fine and the accumulation the whole problem, which is the God "
            "Class shape and is invisible to every other gate OXN has. `GraphStore` is the "
            "local example, WMC 64 with a worst method of 6"
        ),
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
        value=float(thresholds.TRIVIAL_HELPER),
        evidence=Evidence.MEASURED,
        observations=2,
        provenance=(
            "the median helper score separating one cohesive extraction (4, 9, 11) from one "
            "hand-built shred (16 helpers, median 1), both of `_imported_names`. Selects the "
            "shape of a shred only: the cognitive ceiling makes the blocking decision, so "
            "this number cannot by itself fail a build"
        ),
        fit_when=(
            "benchmarks/dogfood-log.jsonl holds ~50 labelled extractions; at that point this "
            "is a grid search over one scalar against judge and human labels. Three known "
            "tripwire edges are its job, not the rule's: helpers written just above the "
            "threshold, helpers given a public name, and helpers given a second call site"
        ),
    ),
    Parameter(
        name="MANY_HELPERS",
        value=float(thresholds.MANY_HELPERS),
        evidence=Evidence.JUDGEMENT,
        observations=0,
        provenance=(
            "the smallest count that can mean 'many'; a three-way dispatch legitimately "
            "extracts three, so the count alone was never going to be the discriminator"
        ),
        fit_when="the same labelled set as TRIVIAL_HELPER",
    ),
)


#: Retrieval and budgeting (P8, ADR-0006). These tune what an agent is *shown*; unlike every
#: parameter above them, no value here can fail a build, so being wrong costs relevance
#: rather than trust. They are listed anyway: a number that hides inside a scoring function
#: is exactly the folklore this module exists to prevent.
_RETRIEVAL: tuple[Parameter, ...] = (
    Parameter(
        name="BM25_K1",
        value=float(thresholds.BM25_K1),
        evidence=Evidence.LITERATURE,
        observations=0,
        provenance="Robertson & Zaragoza (2009) report [1.2, 2.0] as the usual band for k1",
        fit_when="the labelled task -> ADR pairs of ADR-0006 section 5 exist",
    ),
    Parameter(
        name="BM25_B",
        value=float(thresholds.BM25_B),
        evidence=Evidence.LITERATURE,
        observations=0,
        provenance="Robertson & Zaragoza (2009); 0.75 is the standard length-normalization",
        fit_when="the same labelled pairs; ADR length varies 4k-18k chars, so b is load-bearing",
    ),
    Parameter(
        name="MAX_BUNDLE_CONSTRAINTS",
        value=float(thresholds.MAX_BUNDLE_CONSTRAINTS),
        evidence=Evidence.JUDGEMENT,
        observations=0,
        provenance=(
            "constraint decay (arXiv 2605.06445) shows the full set is harmful and says "
            "nothing about where the knee is; 7 is small enough to read and large enough to "
            "carry a file's ceilings plus the decisions scoping it"
        ),
        fit_when=(
            "P11 runs the arms; the bundle size is a factor there, so this is the one "
            "parameter here with an experiment already designed to fit it"
        ),
    ),
)


#: Enforcement policy (P9, ADR-0003 section 4). Unlike the retrieval group, a wrong value
#: here has teeth: too low halts an agent that was converging, too high spends a token
#: budget on a repair that was never going to land.
_ENFORCEMENT: tuple[Parameter, ...] = (
    Parameter(
        name="RETRY_BUDGET",
        value=float(thresholds.RETRY_BUDGET),
        evidence=Evidence.JUDGEMENT,
        observations=0,
        provenance=(
            "3, the smallest count that lets an agent fail, read the increment trail and "
            "try a different shape. arXiv 2508.11958 establishes that the loop needs a "
            "bound -- LLM refactoring often fails to reach the threshold at all -- but "
            "measures whether a repair lands, not how many attempts are worth paying for"
        ),
        fit_when=(
            "the attempt trajectories this budget already records are collected across "
            "sessions. The question is empirical and cheaply answered: of the repairs that "
            "eventually succeed, what fraction needed a third attempt or a fourth? A budget "
            "set below that quantile halts work that would have converged"
        ),
    ),
)


def parameters() -> list[Parameter]:
    """The whole tunable surface. Adding a threshold anywhere means adding it here.

    Data at module level rather than inside a function body, because `oxn check` flagged the
    91-line function this used to be on the day it was written -- and a function whose entire
    body is a list is a list.
    """
    return [*_CEILINGS, *_ANTI_GAMING, *_RETRIEVAL, *_ENFORCEMENT]


def summary() -> dict[str, object]:
    """The parameter surface as data, for a report or a future optimizer to consume."""
    values = parameters()
    return {
        "parameters": [parameter.as_dict() for parameter in values],
        "provisional": sum(1 for parameter in values if parameter.is_provisional),
        "total": len(values),
        "note": (
            "No parameter here is corpus-calibrated, and the ceilings are not going to be. "
            "P10 measured the five threshold corpora: LOC-weighted (Alves et al.) the "
            "ceilings already sit at P79-P100, so the distributions corroborate them, but "
            "the weighted p90 ranges 7 to 23 across five repositories -- a fitted gate "
            "tracks the corpus set. What the corpora give instead is exceedance, the "
            "fraction of real code each ceiling rejects, recorded in every provenance and "
            "changing no value. The anti-gaming parameters still want dogfood labels."
        ),
    }
