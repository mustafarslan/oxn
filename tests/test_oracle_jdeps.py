"""The Java import graph against jdeps, the JDK's own bytecode dependency reader.

P4's exit criterion asks for graph-equality against an independent tool per language: grimp
for Python, "dependency-cruiser and jdeps follow". This is the jdeps half, and it is the
strongest of the three comparisons despite the smallest corpus, because of *which* direction
holds exactly: **every edge OXN records, jdeps also sees.** OXN invents nothing. That is the
direction that matters -- a wrong edge corrupts every downstream metric silently, where a
missing one is merely absent, which is the rule `docs/divergences.md` states throughout.

The other direction does not hold, and cannot: jdeps reads *bytecode*, so it sees the types
the compiler resolved, while OXN reads *imports*, so it sees the types the source names.
Those are different sets whenever inference or erasure puts a type in the class file that
never appeared in the source -- see `_EXPLAINED`, where all three instances are that rule.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.oracle

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "benchmarks" / "corpora" / "java-spring-petclinic"
BUILT = (CORPUS / "target" / "classes", CORPUS / "target" / "test-classes")
SOURCE_ROOTS = ("src/main/java/", "src/test/java/")

#: Both ends of an edge must sit under this package, which is what drops the JDK, Spring and
#: jdeps' own per-archive summary lines (`classes -> java.base`) without a rule for any of
#: them. Everything jdeps cannot place comes back `not found`, and is external by definition.
PACKAGE = "org.springframework.samples.petclinic"

requires_jdeps = pytest.mark.skipif(
    shutil.which("jdeps") is None, reason="jdeps not found; a JDK provides it"
)
requires_build = pytest.mark.skipif(
    not all(path.exists() for path in BUILT),
    reason=(
        "petclinic is not compiled; corpora are cloned, never built "
        "(scripts/fetch_corpora.py). Run `./mvnw -o test-compile` in the corpus -- "
        "the P5 Java SCIP measurement already pays this cost."
    ),
)


def _qualified(relative: str) -> str:
    """`src/test/java/a/B.java` -> `a.B`, the name jdeps prints for the class it compiles to."""
    for root in SOURCE_ROOTS:
        if relative.startswith(root):
            return relative[len(root) : -len(".java")].replace("/", ".")
    raise AssertionError(f"{relative} is under no known source root; layout changed")


def _assert_build_is_current(sources: set[str]) -> None:
    """Fail -- never skip -- when the bytecode does not match the sources beside it.

    `target/` is a gitignored build artifact and can be from any commit. jdeps run over stale
    bytecode diverges from OXN run over current source and looks exactly like a resolver bug,
    so this is checked before anything is compared. A skip would make that invisible forever,
    which is the opposite of what an oracle is for; a failure names the command that fixes it.
    """
    compiled = {
        str(path.relative_to(directory).with_suffix("")).replace("/", ".")
        for directory in BUILT
        for path in directory.rglob("*.class")
        if "$" not in path.name
    }
    if compiled != sources:
        missing = sorted(sources - compiled)[:5]
        extra = sorted(compiled - sources)[:5]
        pytest.fail(
            f"bytecode is stale: {len(sources - compiled)} source(s) uncompiled {missing}, "
            f"{len(compiled - sources)} class(es) with no source {extra}. "
            "Rebuild with `./mvnw -o test-compile` in the corpus."
        )


def _their_edges() -> set[tuple[str, str]]:
    """jdeps' in-package class-to-class edges, read from the compiled bytecode."""
    finished = subprocess.run(  # noqa: S603 - fixed argv, corpus path from this file
        ["jdeps", "-verbose:class", *(str(path) for path in BUILT)],
        capture_output=True,
        text=True,
        check=False,
    )
    if not finished.stdout:
        pytest.fail(f"jdeps produced nothing: {finished.stderr.strip()[:400]}")
    edges = set()
    for line in finished.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[1] != "->":
            continue
        # `Outer$Inner` is one compilation unit with `Outer`, which is the granularity OXN
        # works at: one node per file.
        source, target = parts[0].split("$")[0], parts[2].split("$")[0]
        if source.startswith(PACKAGE) and target.startswith(PACKAGE) and source != target:
            edges.add((source, target))
    return edges


def _our_edges(cache: Path) -> tuple[set[tuple[str, str]], set[str]]:
    """OXN's in-tree import edges as qualified class names, and the sources behind them.

    The file set comes back with the edges because the two must not be derived separately:
    every wrong answer the dependency-cruiser comparison produced came from measuring one
    tool over a set the other never saw.
    """
    from oxn.graph.depgraph import build_dependency_graph
    from oxn.graph.indexer import Indexer

    with Indexer(root=CORPUS, cache_path=cache) as indexer:
        files = list(indexer.sources([CORPUS]))
        parsed = {indexer.relative(path) for path in files if path.suffix == ".java"}
        graph = build_dependency_graph(CORPUS, files)
    edges = {
        (_qualified(source), _qualified(target))
        for source, targets in graph.files.items()
        for target in targets
        if source in parsed and target in parsed
    }
    return edges, {_qualified(path) for path in parsed}


@requires_jdeps
@requires_build
@pytest.mark.skipif(not CORPUS.exists(), reason="corpora not fetched")
def test_oxn_invents_no_java_edge_jdeps_cannot_see(tmp_path: Path) -> None:
    """Measured 2026-09-17 on `java-spring-petclinic`, main and test sources, 50 classes.

    **jdeps 27 edges, OXN 24, agreed 24 -- OXN is a strict subset, 88.9%.** The containment
    is asserted separately and unconditionally below, because it is the invariant rather than
    a ratio: an import-based resolver that started guessing would break it immediately, and no
    agreement floor placed anywhere would catch that on a corpus this small.

    The population is small for an honest reason worth recording: petclinic's classes depend
    on *Spring*, not on each other, so only 27 in-package edges exist at all. The rule that
    diverges -- a type the compiler resolved but the source never named -- is exercised by
    exactly the three edges in `_EXPLAINED`, each verified in bytecode with `javap`.
    """
    ours, sources = _our_edges(tmp_path / "graph.db")
    _assert_build_is_current(sources)
    theirs = _their_edges()
    assert theirs, "jdeps resolved nothing in-package; the corpus or the build is wrong"

    # The invariant. Not a ratio, and not merged with the other direction -- the asymmetry is
    # the finding. OXN reads imports; every import names a type the compiler must also have
    # resolved, so an edge jdeps cannot see is an edge OXN made up.
    invented = ours - theirs
    assert not invented, (
        f"{len(invented)} Java edge(s) OXN records that the compiler did not: {sorted(invented)}"
    )

    missed = theirs - ours
    unexplained = missed - _EXPLAINED
    assert not unexplained, (
        f"{len(unexplained)} edge(s) jdeps sees with no named cause: {sorted(unexplained)}"
    )
    # An explanation that no longer describes anything is how this rots: the corpus moved, or
    # resolution improved, and the list quietly stopped being checked.
    stale = _EXPLAINED - missed
    assert not stale, f"{len(stale)} explanation(s) describe no divergence: {sorted(stale)}"

    # A floor under the measured 88.9%, catching the wholesale case the assertions above
    # cannot: a change that makes both sets small and agreeing.
    agreement = len(ours & theirs) / len(ours | theirs)
    assert agreement >= 0.85, f"{agreement:.1%} agreement over {len(sources)} classes"


#: Edges jdeps sees and OXN does not, measured 2026-09-17. **All three are one rule**, and it
#: is not a defect on either side: jdeps reads bytecode and sees the type the *compiler*
#: resolved; OXN reads imports and sees the type the *source* names. Where inference or
#: erasure puts a type in the class file that never appeared in the source, no import exists
#: for any import-based tool to find. Each was confirmed with `javap -v` at the site:
#:
#: 1. **A return type, never named.** `PetClinicConcurrencyTests` calls `owner.getPet(name)`;
#:    the invoked method's descriptor is `(String)Lowner/Pet;`, so `Pet` is in the constant
#:    pool. The test imports `Owner` and `OwnerRepository` and never mentions `Pet`.
#: 2. **A generic bound, erased into the descriptor.** `ClinicServiceTests` calls
#:    `EntityUtils.getById(vets, Vet.class, 3)`, declared `<T extends BaseEntity> T getById`.
#:    Erasure makes the return descriptor `BaseEntity`; the source names neither it nor
#:    anything in `model`.
#: 3. **An inferred element type, reached through a checkcast.** `ClinicServiceTests` calls
#:    `vet.getSpecialties().get(0).getName()`. `get` erases to `Object`, so the compiler
#:    inserts a cast to `Specialty` and a `Specialty.getName` methodref -- a type the source
#:    never writes down.
#:
#: The set may only shrink by fixing something, and the test fails on an entry that no longer
#: diverges as loudly as on one that newly does.
_EXPLAINED: frozenset[tuple[str, str]] = frozenset(
    {
        (f"{PACKAGE}.PetClinicConcurrencyTests", f"{PACKAGE}.owner.Pet"),
        (f"{PACKAGE}.service.ClinicServiceTests", f"{PACKAGE}.model.BaseEntity"),
        (f"{PACKAGE}.service.ClinicServiceTests", f"{PACKAGE}.vet.Specialty"),
    }
)
