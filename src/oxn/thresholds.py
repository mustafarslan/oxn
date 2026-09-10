"""Default thresholds, and the reasoning behind each one.

These are *defaults*, not policy. `oxn.yaml` overrides every value (P7), and the
self-calibrating ratchet of P10 is expected to be the better choice on any codebase that
predates OXN -- a fixed ceiling fails on the first commit of a legacy repo and gets the
tool switched off.

Each value below records where it came from. A threshold with no stated provenance is
folklore, and folklore is what makes developers distrust a gate.
"""

from __future__ import annotations

from typing import Final

#: Cognitive complexity ceiling for one function.
#:
#: **12, chosen deliberately.** `idea.md` proposed 8; SonarSource's own default is 15. 8 is
#: nearly twice as strict as the metric's authors recommend and would flag a great deal of
#: reasonable code; 15 is permissive enough that agent-written functions routinely pass it
#: while still being hard to read. 12 sits between them, closer to the published default
#: than to the original guess.
#:
#: Note the interaction with anti-gaming (docs/metrics.md section 10.5): a ceiling on its
#: own teaches an agent to shred one function into twenty one-line helpers. It is only safe
#: paired with the class- and module-level aggregates that land in P10.
MAX_COGNITIVE_COMPLEXITY: Final[int] = 12

#: Cyclomatic complexity ceiling. McCabe's own 1976 suggestion; NIST SP 500-235 discusses
#: 10 and 15 as the usual band.
MAX_CYCLOMATIC_COMPLEXITY: Final[int] = 10

#: Long Parameter List (Fowler, *Refactoring*); the 4-5 band traces to Martin, *Clean Code*.
MAX_PARAMETERS: Final[int] = 5

#: Deep nesting is the single strongest readability signal cognitive complexity encodes.
MAX_NESTING_DEPTH: Final[int] = 4

#: Function and file length, in source lines. Conventional rather than derived. P10 measured
#: what they cost rather than replacing them: see `oxn.calibration`, and ROADMAP's P10
#: amendment for why a corpus percentile is not a ceiling.
MAX_FUNCTION_SLOC: Final[int] = 60
MAX_FILE_SLOC: Final[int] = 500

#: Class aggregates, and the pair `docs/metrics.md` section 10.5 asks for so that work spread
#: across a class is caught rather than only work spread across one function's helpers.
#:
#: Both sit inside a window a control defines rather than at a percentile: the two evasions
#: `shredding` cannot see -- a shred with public names, and a shred whose helpers gained a
#: second caller -- score NOM 14 and WMC 27, while the legitimate decomposition of the same
#: work scores 4 and 17 (`tests/test_class_scope_evasion.py`). Anything in 5..13 and 18..26
#: separates them; these are the round numbers in that window, deliberately not its edges,
#: because a value tuned to straddle one fixture is fitted to that fixture.
#:
#: These reject 0.00-7.48% and 0.27-10.28% of real classes across the six corpora
#: (`benchmarks/ceiling-observations.json`, 3,501 classes) -- overlapping the per-function
#: ceilings' 0.00-10.57% rather than sitting above it, because the evasions sit *inside* the
#: legitimate distribution and not outside it. They are therefore a **ratchet** first:
#: `.oxn/baseline.json` absorbs the classes that were already large, and what blocks is a
#: class getting worse.
MAX_METHODS_PER_CLASS: Final[int] = 12
MAX_WEIGHTED_METHODS_PER_CLASS: Final[int] = 25

# ---- Anti-gaming: satisfying a ceiling without simplifying anything --------------------
#
# These two select the *shape* of a shred; they never block on their own. What blocks is the
# cluster total against MAX_COGNITIVE_COMPLEXITY above -- see `oxn.metrics.shredding`.

#: A helper scoring at or below this is *trivial*: a line with a name rather than a unit of
#: work. Calibrated on n=2 (one cohesive extraction scoring 4/9/11, one hand-built shred of
#: the same function scoring a median of 1), which is disclosed rather than implied --
#: `oxn calibration` prints the evidence count for every number here.
TRIVIAL_HELPER: Final[float] = 2.0

#: How many dedicated helpers before a split is worth examining. Three is the smallest count
#: that can mean "many", and a three-way dispatch legitimately extracts three -- which is
#: why the count alone was never the discriminator.
MANY_HELPERS: Final[int] = 3

# ---- Tier 1.5: volume and erosion -----------------------------------------------------

#: Minimum token run for a duplicate to count. PMD-CPD defaults to 100; OXN uses 50 with a
#: line guard, because 100 misses the small copy-paste blocks agents actually produce.
DUPLICATION_MIN_TOKENS: Final[int] = 50

#: A duplicate must also span at least this many lines, which suppresses long single-line
#: expressions and idiomatic boilerplate.
DUPLICATION_MIN_LINES: Final[int] = 4

#: Share of functions whose complexity mass defines `erosion@k`. 10% is SlopCodeBench's
#: framing: "what fraction of the complexity lives in the worst tenth of the code".
EROSION_TOP_FRACTION: Final[float] = 0.10

#: Fraction of a codebase that may be duplicated before it is worth reporting.
MAX_DUPLICATION_RATIO: Final[float] = 0.05

# ---- Tier 2.5: history ----------------------------------------------------------------

#: Minimum co-change count before a file pair is called coupled. Below this, coincidence
#: dominates (Zimmermann et al., TSE 2005).
CHANGE_COUPLING_MIN_SUPPORT: Final[int] = 5

#: Commits touching more than this many files are excluded from coupling analysis: a
#: reformat or a squashed merge otherwise couples everything to everything.
CHANGE_COUPLING_MAX_COMMIT_SIZE: Final[int] = 30

#: Confidence below which a coupling is not worth reporting.
CHANGE_COUPLING_MIN_CONFIDENCE: Final[float] = 0.3

#: An author holding less than this share of a file's commits is a *minor* contributor.
#: Bird et al. (FSE 2011) found the count of minor contributors the strongest defect
#: correlate they measured.
MINOR_CONTRIBUTOR_SHARE: Final[float] = 0.05

# ---- Tier 2: architecture ---------------------------------------------------------------

#: Fan-in plus fan-out percentile above which a component is hub-shaped. Arcan derives its
#: threshold adaptively from the system plus a benchmark corpus; OXN uses the project's own
#: p90 and says so, since no public percentile table exists for Python or TypeScript.
HUB_PERCENTILE: Final[float] = 0.90

#: A hub must also be *balanced* -- genuinely central rather than merely popular -- so both
#: directions need at least this degree, and the smaller must be at least half the larger.
HUB_MIN_DEGREE: Final[int] = 5

#: Degree of Unstable Dependency: the share of a component's dependencies that are less
#: stable than it is. 30% comes from Fontana et al., *ICSME 2016*, chosen by manual
#: validation across projects.
UNSTABLE_DEPENDENCY_RATIO: Final[float] = 0.30

#: Floor for the God Component threshold, below which "large" is not meaningful. Lippert &
#: Rook (*Refactoring in Large Software Projects*, 2006) use a fixed 27,000 lines; Arcan
#: prefers an adaptive value. OXN takes the project's p90 but never less than this.
GOD_COMPONENT_MIN_LOC: Final[int] = 5000

# ---- P8: retrieval and the constraint bundle -------------------------------------------
#
# These three tune *what an agent is shown*, never what the gate accepts (ADR-0006 section
# 1). A wrong value here costs relevance; it cannot fail or pass a build.

#: Okapi BM25 term-frequency saturation. Above `k1` occurrences a term stops adding much,
#: which is what keeps a long ADR from outranking a precise one by repetition. 1.5 is the
#: value Robertson & Zaragoza report as the usual [1.2, 2.0] choice (*The Probabilistic
#: Relevance Framework*, 2009); it is not tuned on this corpus and cannot be until the
#: labelled task -> ADR pairs of ADR-0006 section 5 exist.
BM25_K1: Final[float] = 1.5

#: How much document length is normalized away, from 0 (not at all) to 1 (fully). 0.75 is
#: the same source's default. It matters more here than the literature assumes: ADRs in one
#: corpus range from 4k to 18k characters, so length normalization is doing real work rather
#: than trimming an edge case.
BM25_B: Final[float] = 0.75

#: How many constraints a bundle may carry. Constraint decay (arXiv 2605.06445) says the
#: full set is actively harmful and says nothing about where the knee is; 7 is a judgement
#: pending the arms of P11, chosen because it is small enough to be read and large enough to
#: carry the ceilings that govern a file plus the decisions that scope it.
#:
#: The cap is a display policy and never an enforcement one: the gate still checks every
#: constraint, shown or not.
MAX_BUNDLE_CONSTRAINTS: Final[int] = 7

# ---- P9: the bounded remediation loop ---------------------------------------------------

#: How many times OXN will report the *same* violation to the same agent session before it
#: stops asking for a repair and reports the failure instead (ADR-0003 section 4).
#:
#: The cap exists because the loop is not guaranteed to converge: "Clean Code, Better
#: Models" (arXiv 2508.11958) finds LLM refactoring frequently fails to get under a
#: complexity threshold at all. Without a bound, a gate that keeps saying no to an agent
#: that keeps saying yes is an infinite loop with a token budget attached.
#:
#: 3 is a judgement, and a deliberately unfitted one: it is the smallest number that lets an
#: agent fail, read the increment trail, and try a genuinely different shape. There is no
#: literature on the right number of retries for this loop -- what exists (2508.11958)
#: measures whether the repair lands, not how many attempts help -- so this is listed in
#: `oxn calibration` with zero observations like every other provisional value.
#:
#: Counted per (session, finding), never per file: an agent editing three violations in one
#: file is making progress on each of them independently.
RETRY_BUDGET: Final[int] = 3
