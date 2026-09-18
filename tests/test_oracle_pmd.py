"""Duplication against PMD-CPD, the reference copy-paste detector, in five languages.

**This file measured one corpus in one language until it measured five.** The recall number
it pinned -- 92% of PMD's duplicated lines -- came from httpx, and `DUPLICATION_MIN_TOKENS`
carried `observations=1` and a `fit_when` saying the value could not be fitted "until clone
recall is measured across more than one corpus" precisely because of that. Widening it found
a detector bug that one Python package could not show: see `_select` in `oxn/volume/clones.py`
and `docs/divergences.md`.

The helpers live in `scripts/measure_duplication.py` and are loaded rather than duplicated
here, for the reason `measure_semantic.py` loads the citation extractor: two copies of "what
counts as a file both tools analysed" is how a measurement and its test come to disagree
about what was measured.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.oracle

ROOT = Path(__file__).resolve().parent.parent
CORPORA = ROOT / "benchmarks" / "corpora"
PMD = Path(os.environ.get("OXN_PMD_DIR", ROOT / "tools" / "pmd-bin")) / "bin" / "pmd"

#: Recall floors per corpus, each a measured value rounded down to the nearest 0.01 -- not a
#: target anybody aimed at. They differ by language because the *divergence* does: OXN reports
#: one maximal non-overlapping class where PMD reports several overlapping ones, and how much
#: that costs depends on how repetitive the corpus is. A floor per language says that; one
#: floor for all five would either be unreachable for TypeScript or vacuous for Python.
FLOORS = {
    "python-httpx": 0.89,
    "java-spring-petclinic": 0.85,
    "go-kit": 0.83,
    "javascript-eslint": 0.86,
    "typescript-nest": 0.80,
}

requires_pmd = pytest.mark.skipif(
    not PMD.exists(), reason="PMD not installed; scripts/check.py --oracle --install"
)


def _measure() -> object:
    """`scripts/measure_duplication.py`, loaded the way the retrieval tests load their script."""
    spec = importlib.util.spec_from_file_location(
        "measure_duplication", ROOT / "scripts" / "measure_duplication.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["measure_duplication"] = module
    spec.loader.exec_module(module)
    return module


@requires_pmd
@pytest.mark.slow
@pytest.mark.parametrize("corpus", sorted(FLOORS))
def test_duplication_recovers_what_pmd_cpd_finds(corpus: str) -> None:
    """Recall against PMD-CPD, not equality, in every language CPD has a lexer for.

    The two tools tokenize differently and resolve overlapping candidates differently, so
    exact clone-class equality was never a meaningful target. What matters for a duplication
    detector is what it *misses*, and missing it in one language only is the failure this
    parametrization exists to catch: before the `_select` fix, recall ran from 0.69 in
    TypeScript to 0.86 in Python, and the single Python assertion here saw none of it.
    """
    if not (CORPORA / corpus).exists():
        pytest.skip("corpora not fetched")
    measure = _measure()
    loaded = measure.load(corpus)
    row = measure._measure_at(loaded, 50)
    assert row.pmd_lines, f"PMD reported nothing on {corpus}; the oracle is misconfigured"
    assert row.recall >= FLOORS[corpus], (
        f"{corpus}: recall {row.recall:.3f} of PMD's {row.pmd_lines} duplicated lines, "
        f"against a floor of {FLOORS[corpus]}"
    )


@requires_pmd
@pytest.mark.slow
def test_duplication_agrees_with_pmd_on_which_files_are_affected() -> None:
    """File-level agreement on httpx, where the recorded divergence table was measured.

    Kept on one corpus on purpose: file-level Jaccard is a much coarser statement than recall
    and it moves with how a repository splits its modules, not with the detector.
    """
    if not (CORPORA / "python-httpx").exists():
        pytest.skip("corpora not fetched")
    measure = _measure()
    row = measure._measure_at(measure.load("python-httpx"), 50)
    assert row.file_jaccard >= 0.55, f"file-level Jaccard {row.file_jaccard:.2f}"
