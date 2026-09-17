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
    #: Still zero where nobody has measured. `BM25_K1` and `BM25_B` were the entry this note
    #: named as obvious and are filled in now, at 153 pairs each -- which is what turned the
    #: pair of them from "waiting for labels" into two different answers: k1 is flat across
    #: its literature band, and b is load-bearing with the two label sets disagreeing on the
    #: direction. Neither moved.
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
        observations=14291,
        provenance=(
            "12, between idea.md's proposed 8 and SonarSource's default 15. Neither endpoint "
            "is measured either: 15 is a product default, not a finding. Measured cost: it "
            "rejects 0.53% (java) to 10.57% (javascript) of named callables across the six corpora."
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
        observations=14291,
        provenance=(
            "McCabe (1976); NIST SP 500-235 discusses 10 and 15 as the usual band. Measured "
            "cost: rejects 0.48% (typescript) to 5.11% (javascript) of named callables."
        ),
        fit_when="a published threshold with a corpus behind it, rather than a band",
    ),
    Parameter(
        name="MAX_PARAMETERS",
        value=float(thresholds.MAX_PARAMETERS),
        evidence=Evidence.LITERATURE,
        observations=14291,
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
        observations=14291,
        provenance=(
            "conventional; nesting is what cognitive complexity already charges for. Measured "
            "cost: rejects 0.00% (go, java) to 0.46% (javascript) of named callables -- the least "
            "load-bearing ceiling here by an order of magnitude, which is consistent with it "
            "being double-charged. It was ten times that until the depth of an `else if` "
            "stopped depending on the grammar: TypeScript and Rust wrap it in an "
            "`else_clause`, and counting the wrapper *and* the `if` it holds rejected ripgrep "
            "at 0.96% against Python's 0.35% for the same shape. Cognitive complexity had the "
            "rule right all along; this metric reused its node set and re-derived its rule. "
            "**The question `fit_when` asked has been answered, and the answer is no.** "
            "Across the six corpora at default ceilings this rule flags 25 entities and "
            "**every one of them is already flagged by another rule** -- all 25 by "
            "`cognitive_complexity`, 18 also by `cyclomatic_complexity`. It is the only "
            "gated rule with no catch of its own: the next lowest, `shredding`, has 1 of 3, "
            "and `cyclomatic_complexity` -- the one the literature calls redundant -- keeps "
            "98 of 351. As a *gate* it spends a constraint to repeat a finding; as a line in "
            "the remediation it is the most actionable thing the cognitive trail says, which "
            "is why it stays measured and reported."
        ),
        fit_when=(
            "answered rather than open: 0 unique catches of 25 over six corpora. What would "
            "reopen it is a fix-rate -- whether an agent shown `max_nesting_depth 6` repairs "
            "more often than one shown only `cognitive_complexity 21` -- which is a P11 "
            "measurement and not a corpus one."
        ),
    ),
    Parameter(
        name="MAX_FUNCTION_SLOC",
        value=float(thresholds.MAX_FUNCTION_SLOC),
        evidence=Evidence.JUDGEMENT,
        observations=14291,
        provenance=(
            "conventional rather than derived. Measured cost: rejects 0.53% (java) to 8.16% "
            "(javascript) of named callables."
        ),
        fit_when="the same labelled set as the cognitive ceiling",
    ),
    Parameter(
        name="MAX_FILE_SLOC",
        value=float(thresholds.MAX_FILE_SLOC),
        evidence=Evidence.JUDGEMENT,
        observations=3796,
        provenance=(
            "conventional rather than derived, and the one ceiling the measurement argues "
            "with. Measured cost across 3,796 files: 0.00% (java), 0.39% (go), 1.58% "
            "(typescript), 10.00% (python), 14.96% (javascript), **20.91% (rust)** -- a 50x "
            "spread where every other ceiling holds inside 20x. The Rust files above it are "
            "hand-written core, "
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
        observations=3501,
        provenance=(
            "12, a round number inside a window a control defines rather than a percentile. "
            "The two shred shapes `shredding` cannot see -- public helper names, and helpers "
            "given a second caller -- score NOM 14, while the legitimate decomposition of the "
            "same work scores 4, so anything in 5..13 separates them. Measured across 3,501 "
            "classes: rejects 0.00% (go-kit, whose largest type has 9 methods) to 7.48% "
            "(httpx), and 0.7% of OXN's own 149. "
            "Higher than the per-function ceilings because the evasions sit inside the "
            "legitimate distribution rather than beyond it, which is why this is a ratchet "
            "first. **The population is every class entity, not the named ones.** When "
            "these figures were taken the unnamed ones were where the methods were -- a Rust "
            "`impl` block is an unnamed class entity, ripgrep read 840 of them against 395 "
            "named, and the name filter dropped its exceedance from 3.10% to 0.25% by "
            "measuring struct declarations rather than code. The `impl` join below has since "
            "moved those methods onto the named type, so the unnamed fragments now carry no "
            "aggregate at all; the population stays unfiltered because the gate does not "
            "filter either, and because Go's structs are unnamed entities regardless. "
            "**Re-measured after that join the filter no longer changes the answer**, which "
            "is the join working: ripgrep read 3.10% of 840 unfiltered against 0.25% of 395 "
            "named -- a thirty-fold disagreement about the same code -- and now reads 7.35% "
            "and 7.34% of 408. "
            "**Go needs a receiver join**, since a method there is a top-level "
            "declaration rather than a member of its type: without it every struct measured "
            "NOM 0 and the ceiling was silently inert for the language. The join is by "
            "package, which Go's own rule makes exact -- see `indexer.aggregate_classes`. "
            "**Rust needs an `impl` join** for the same reason and with sharper teeth: a "
            "type's methods were counted per block, so fourteen split seven and seven "
            "walked through this ceiling that the same fourteen failed in one block. That "
            "is a cheaper evasion than any of the shred shapes -- it costs a newline and "
            "the word `impl`, and the result is more idiomatic than the version that fails. "
            "It was found by making the two paths that both compute NOM agree, and the "
            "check that found it is kept."
        ),
        fit_when=(
            "a second evasion pair. Both this and the WMC ceiling are positioned by a single "
            "authored control, which is why neither sits at its window's edge. The other "
            "half of the evidence is not authored: across the six corpora the two ceilings "
            "reject 129 classes, 31 of them by NOM alone and 26 by WMC alone, so neither is "
            "a restatement of the other. Eight of the 129 are test classes, which is the "
            "known false positive -- many small test methods is a legitimate shape, and "
            "`oxn.yaml`'s advisory paths are the answer rather than a looser ceiling. A "
            "second false positive was found by measuring rather than by argument: the "
            "**wide repository**, a class whose method count is a query catalogue over one "
            "connection. OXN's own `GraphStore` is it -- NOM 26, WMC 62, no method above "
            "cyclomatic 6 -- and it reads exactly like the God Class above until LCOM4 is "
            "asked: LCOM4 2, whose second component is `__enter__`, a one-line `return "
            "self`. Every other method touches `self._conn` or routes through "
            "`_transaction`, so it is one thing with 26 doors. Splitting it by subject "
            "buys nothing: the parts would share the connection, and a facade over them "
            "returns the same count. **LCOM4 is the discriminator between the two shapes**, "
            "and a count on its own cannot tell them apart -- which is why these two "
            "ceilings are a ratchet and not a gate. That claim cost a defect to make true: "
            "written against a matched control pair it read 1 component for *both*, because "
            "a constructor assigning every field joins every component through itself. "
            "Constructors are now excluded from the component scan, and the God Class "
            "reports the five collaborators it actually has -- `metrics.cohesion` and "
            "`tests/test_wide_repository.py` carry the argument"
        ),
    ),
    Parameter(
        name="MAX_WEIGHTED_METHODS_PER_CLASS",
        value=float(thresholds.MAX_WEIGHTED_METHODS_PER_CLASS),
        evidence=Evidence.JUDGEMENT,
        observations=3501,
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
            "Class shape and is invisible to every other gate OXN has. That shape is not "
            "the only one over this ceiling, though, and `GraphStore` is the counter-example "
            "rather than the example -- see MAX_METHODS_PER_CLASS on the wide repository"
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
        observations=5,
        provenance=(
            "50 rather than PMD-CPD's 100. Measured 2026-09-17 against PMD-CPD across five "
            "corpora, one per language CPD has a lexer for -- httpx, spring-petclinic, "
            "go-kit, eslint and nest -- by `scripts/measure_duplication.py`. **Raising 50 to "
            "100 discards between 46% and 81% of the duplication PMD reports** (httpx 72%, "
            "petclinic 81%, go-kit 46%, eslint 46%, nest 50%), which is the cost this value "
            "buys and the claim the old one-corpus provenance was making without a number. "
            "Widening from the single corpus also found the defect behind `_select`: a clone "
            "class was refused whole when any one occurrence was already claimed, which cost "
            "recall in every language and most in TypeScript, 0.69 -> 0.82"
        ),
        fit_when=(
            "a corpus of agent-written code exists. The old condition -- more than one "
            "corpus -- is met, and the five above are all human-written open source, while "
            "the reason this number is 50 is a claim about the short blocks *agents* paste. "
            "The cost of 100 is now measured; what is still unmeasured is whether 50 is "
            "better than 40 or 60, and no corpus here can say, because none of them was "
            "written by the thing this threshold is aimed at"
        ),
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
            "threshold, helpers given a public name, and helpers given a second call site. "
            "**It holds one, and the route is costed rather than close.** Counted 2026-09-17: "
            "44 attempts and 5.1 hours of wall clock produced 4 candidates that extracted any "
            "helper at all (9.1%) and 1 that also reached the judge (2.3%) -- the other 39 "
            "never got there, failing tests, lint or types first, and 4 of the 5 accepted "
            "repairs simplified without extracting anything. At that yield ~50 judged "
            "extractions is ~2,200 attempts and ~221 hours of cloud-model time. The number to "
            "move is the yield, not the patience: a harness aimed at functions that invite "
            "extraction, on corpora rather than on this already-clean tree"
        ),
    ),
    Parameter(
        name="MANY_HELPERS",
        value=float(thresholds.MANY_HELPERS),
        evidence=Evidence.JUDGEMENT,
        # The four extractions `benchmarks/dogfood-log.jsonl` has produced, evaluated at 3.
        # Cost at the current value, not a fit -- and four is small enough that it is quoted
        # rather than summarised.
        observations=4,
        provenance=(
            "the smallest count that can mean 'many'; a three-way dispatch legitimately "
            "extracts three, so the count alone was never going to be the discriminator. "
            "Measured on the four extractions logged so far: helper counts 2, 3, 3 and 6. "
            "The count gate alone excluded one of the four, and the only cluster the shape "
            "test flagged was the 6-helper one whose median score was 1.5 -- the 3-helper "
            "candidates were let through by the *median*, which is the discriminator this "
            "provenance says the count was never going to be"
        ),
        fit_when=(
            "the same labelled set as TRIVIAL_HELPER, whose entry now carries what that set "
            "actually costs. This parameter needs less of it than that one: 3 decides whether "
            "two helpers can be a shred, so the labels that bear on it are candidates "
            "extracting exactly two. The log holds one such candidate in 44 attempts"
        ),
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
        observations=153,
        provenance=(
            "Robertson & Zaragoza (2009) report [1.2, 2.0] as the usual band for k1. Measured "
            "2026-09-17 by sweeping all 153 labelled pairs at the current b, and **it barely "
            "matters here**: across the whole band P@1 moves 0.623-0.642 on OXN's 53 pairs "
            "and 0.600-0.610 on the external 100, which is one pair either way. Fitting it "
            "would be chasing noise, and the literature value is already inside the flat part"
        ),
        fit_when=(
            "never, on this evidence. The pairs `fit_when` used to wait for now exist -- 53 "
            "from this repository's history and 100 from five others -- and they say the "
            "parameter is not load-bearing at this corpus size. It would take a labelled set "
            "where k1 separates rankings at all before fitting it could mean anything"
        ),
    ),
    Parameter(
        name="BM25_B",
        value=float(thresholds.BM25_B),
        evidence=Evidence.LITERATURE,
        observations=153,
        provenance=(
            "Robertson & Zaragoza (2009); 0.75 is the standard length-normalization. Measured "
            "2026-09-17 by sweeping all 153 labelled pairs: **b is load-bearing, and the two label "
            "sets disagree about which way.** On OXN's own 53 pairs P@1 falls monotonically, "
            "0.698 at b=0 to 0.547 at b=1; on the external 100 it rises monotonically, 0.560 "
            "to 0.630. Each set's optimum is the other set's worst value"
        ),
        fit_when=(
            "a labelled set whose optimum is not an artefact of the corpus it came from. "
            "This is the ceilings argument again and it arrived by the same route: a fitted "
            "b tracks the label set, and here the two available sets point in opposite "
            "directions, so either fit would be a statement about one repository. OXN's own "
            "corpus holds five decisions, small enough that b=0 merely reproduces the "
            "breadth-prior baseline the retrieval tests already decline to beat; the external "
            "set is the larger evidence and its best value is 0.63 against 0.61 at the "
            "current setting, two pairs in a hundred. Neither is worth moving a shipped "
            "default for"
        ),
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
        # Repair trajectories in `benchmarks/dogfood-log.jsonl`: this parameter's cost at its
        # current value rather than a fit, so `evidence` stays `JUDGEMENT` and 3 has not
        # moved. Counted as runs beginning at `attempt == 1`, because the log grew through
        # seven row shapes and `run`/`arm`/`repeat` are absent from 35 of its 44 rows --
        # keying on them silently collapsed unlike rows into one trajectory and undercounted
        # these as 16.
        observations=24,
        provenance=(
            "3, the smallest count that lets an agent fail, read the increment trail and "
            "try a different shape. arXiv 2508.11958 establishes that the loop needs a "
            "bound -- LLM refactoring often fails to reach the threshold at all -- but "
            "measures whether a repair lands, not how many attempts are worth paying for. "
            "Measured since on the 24 repair trajectories in benchmarks/dogfood-log.jsonl, "
            "which runs the loop this budget governs: 5 landed a repair, 4 of them at the "
            "first attempt and 1 at the second, none at the third. Six trajectories reached "
            "a third attempt and none converged, so at 3 this budget has never cut off a "
            "repair that was going to work -- if anything the third attempt looks unearned"
        ),
        fit_when=(
            "there are enough trajectories to take a quantile. The measurement above is the "
            "right one and n=5 successes is far too few to set a number by; what it wants is "
            "more of them, from varied agents on varied repositories rather than this one -- "
            "P11's grid. The hook *ledger* cannot supply them, and an earlier version of this "
            "entry wrongly said it already did: `retry.charge` rewrites each path with only "
            "what the current run found, so a violation is forgotten at the moment it is "
            "repaired, which is precisely the event a fit needs. `.oxn/cache/repairs.jsonl` "
            "now keeps it -- one append-only line per violation that stopped being reported, "
            "carrying the attempt count and the value trajectory. It costs the hook nothing "
            "on the common path, because a run that repaired nothing has nothing to append: "
            "measured at 390 ms median with and without it, indistinguishable. So real "
            "sessions accumulate the events from here on, and the remaining gap is time and "
            "variety rather than mechanism"
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


def gate_status(settings: object) -> dict[str, bool]:
    """Threshold constant -> whether the rule it governs is on in this project.

    A ceiling is only half a decision. The other half is whether the rule is enabled at all,
    and that was visible nowhere: `oxn calibration` listed `MAX_NESTING_DEPTH = 4` with its
    provenance whether or not this project had switched the rule off. A number that is not
    being applied is not a threshold, it is a default.

    Only the gated rules appear. `BM25_K1` and the retry budget have no on/off.
    """
    from oxn.config import GATED_METRICS

    disabled: frozenset[str] = getattr(settings, "disabled", frozenset())
    return {gate.threshold: rule not in disabled for rule, gate in GATED_METRICS.items()}


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
