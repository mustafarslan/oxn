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

#: Function and file length, in source lines. Conventional rather than derived; P10's
#: percentile calibration is expected to replace both with corpus-derived values.
MAX_FUNCTION_SLOC: Final[int] = 60
MAX_FILE_SLOC: Final[int] = 500

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
