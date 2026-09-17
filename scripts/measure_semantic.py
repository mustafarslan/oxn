"""Does a static embedding beat BM25 on the retrieval labels? It does not.

    pip install model2vec        # the `oxn[semantic]` extra ADR-0001 sanctioned
    python scripts/measure_semantic.py

**This is the measurement that declined `oxn[semantic]`.** P8 planned the extra and deferred
it "until it can be compared against the first ranker on the labels P8 built" -- 53 pairs from
this repository's history and 100 citation-derived pairs from five others. Those labels now
exist, so the deferral came due, and the rule is the same one that kept `networkx` and
`rank-bm25` out: adopt a dependency against numbers or not at all.

Measured 2026-09-17 with `minishlab/potion-base-8M`, 256-dimensional static vectors, cosine
similarity over the same decision text BM25 indexes:

=========================  =====  =====  =====
external, 100 pairs          P@1    P@3    MRR
=========================  =====  =====  =====
BM25                       0.610  0.820  0.726
model2vec                  0.440  0.630  0.568
BM25 + model2vec (RRF)     0.450  0.730  0.634
=========================  =====  =====  =====

=========================  =====  =====  =====
OXN's own, 53 pairs          P@1    P@3    MRR
=========================  =====  =====  =====
BM25                       0.623  0.868  0.760
model2vec                  0.509  0.774  0.677
BM25 + model2vec (RRF)     0.491  0.830  0.684
=========================  =====  =====  =====

**It loses on both label sets, and fusing the two is worse than BM25 alone** -- which is the
result that closes the question rather than merely answering it. A ranker that lost while
contributing something BM25 lacks would be worth a hybrid; this one drags the fusion down,
so there is nothing to recover.

Why it should have been expected, stated after the fact rather than instead of the
measurement: ADR-0006 section 5a already found that OXN's own labels ask which decisions'
*scope* a change falls under, and an embedding has no more scope than BM25 does. On the
external labels the task is to match a commit message to the decision it is about, and those
share literal vocabulary -- identifiers, file names, the decision's own title -- which is
exactly what term matching is good at and what a 256-dimensional static vector blurs.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CORPORA = ROOT / "benchmarks" / "corpora"
EXTERNAL = ROOT / "benchmarks" / "retrieval-labels-external.json"
OWN_LABELS = ROOT / "benchmarks" / "retrieval-labels-oxn.json"
OWN_CORPUS = ROOT / "benchmarks" / "retrieval-corpus-oxn.json"

#: Small, static, no torch -- the whole reason ADR-0001 admitted this family rather than
#: ChromaDB and sentence-transformers.
MODEL = "minishlab/potion-base-8M"


def _model() -> Any:
    try:
        from model2vec import StaticModel
    except ImportError:
        raise SystemExit("model2vec is not installed: pip install model2vec") from None
    return StaticModel.from_pretrained(MODEL)


def _unit(model: Any, texts: list[str]) -> Any:
    """Embeddings on the unit sphere, so a dot product is a cosine."""
    import numpy as np

    vectors = np.asarray(model.encode(texts))
    return vectors / (np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-9)


def _labels_module() -> Any:
    """The citation extractor, loaded the way `tests/test_retrieval_external.py` loads it.

    One definition of what a query is: duplicating it here is how a measurement and the thing
    it measures come to disagree about what was measured.
    """
    spec = importlib.util.spec_from_file_location(
        "citation_labels", ROOT / "scripts" / "extract_citation_labels.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["citation_labels"] = module
    spec.loader.exec_module(module)
    return module


def _score(rows: list[dict[str, Any]], key: str) -> tuple[float, float, float]:
    """Precision@1, precision@3 and MRR, rounded to where a hundred pairs can support them."""
    total = len(rows)
    at_one = sum(any(name in row["gold"] for name in row[key][:1]) for row in rows)
    at_three = sum(any(name in row["gold"] for name in row[key][:3]) for row in rows)
    reciprocal = sum(
        next((1 / rank for rank, name in enumerate(row[key], 1) if name in row["gold"]), 0.0)
        for row in rows
    )
    return round(at_one / total, 3), round(at_three / total, 3), round(reciprocal / total, 3)


def _fuse(row: dict[str, Any]) -> list[Any]:
    """Reciprocal-rank fusion of the two orderings, the cheapest way to ask whether the
    embedding knows anything BM25 does not."""
    fused: dict[Any, float] = {}
    for key in ("bm25", "semantic"):
        for rank, identifier in enumerate(row[key], 1):
            fused[identifier] = fused.get(identifier, 0.0) + 1.0 / (60 + rank)
    return [identifier for identifier, _ in sorted(fused.items(), key=lambda pair: -pair[1])]


@dataclass(frozen=True)
class Corpus:
    """One repository's decisions, indexed both ways. A pair is only ranked against its own.

    The same shape `tests/test_retrieval_external.py` uses, for the same reason: building the
    index and ranking a task against it are two jobs, and the loop that does both at once is
    the one OXN's own gate rejected at cognitive 20.
    """

    number: dict[str, int]
    identifiers: list[str]
    matrix: Any
    index: Any


def _corpus(model: Any, root: Path, adr_dir: str) -> Corpus:
    from oxn.context.bm25 import BM25
    from oxn.context.decisions import decision_documents
    from oxn.rules.adr import load_decisions

    decisions = list(load_decisions(root, Path(adr_dir)))
    documents = list(decision_documents(decisions))
    return Corpus(
        number={d.identifier: int(re.sub(r"\D", "", d.identifier) or -1) for d in decisions},
        identifiers=[document.identifier for document in documents],
        matrix=_unit(model, [document.text for document in documents]),
        index=BM25(documents),
    )


def _query(extractor: Any, root: Path, sha: str) -> str:
    """The commit's task text, exactly as the label extractor defines it."""
    fields = extractor.git(root, "log", "-1", "--format=%s\x1f%b", sha).split("\x1f")
    return extractor.task_text(fields[0].strip(), fields[1] if len(fields) > 1 else "")


def external(model: Any) -> list[dict[str, Any]]:
    """Every citation-derived pair, ranked against its own repository's decisions."""
    import numpy as np

    extractor = _labels_module()
    labels = json.loads(EXTERNAL.read_text())
    rows: list[dict[str, Any]] = []
    for name, spec in labels["corpora"].items():
        root = CORPORA / name
        corpus = _corpus(model, root, spec["adr_dir"])
        for pair in spec["pairs"]:
            task = _query(extractor, root, pair["sha"])
            similarity = corpus.matrix @ _unit(model, [task])[0]
            rows.append(
                {
                    "gold": set(pair["gold"]),
                    "bm25": [corpus.number[hit.identifier] for hit in corpus.index.rank(task)],
                    "semantic": [
                        corpus.number[corpus.identifiers[i]] for i in np.argsort(-similarity)
                    ],
                }
            )
    for row in rows:
        row["hybrid"] = _fuse(row)
    return rows


def own(model: Any) -> list[dict[str, Any]]:
    """OXN's own pairs against the frozen corpus, where the gold is *scope*."""
    import numpy as np

    from oxn.context.bm25 import BM25, Document

    frozen = json.loads(OWN_CORPUS.read_text())["documents"]
    identifiers = sorted(frozen)
    matrix = _unit(model, [frozen[name] for name in identifiers])
    index = BM25([Document(name, frozen[name]) for name in identifiers])
    rows = []
    for pair in json.loads(OWN_LABELS.read_text())["pairs"]:
        similarity = matrix @ _unit(model, [pair["task"]])[0]
        rows.append(
            {
                "gold": set(pair["gold"]),
                "bm25": [hit.identifier for hit in index.rank(pair["task"])],
                "semantic": [identifiers[i] for i in np.argsort(-similarity)],
            }
        )
    for row in rows:
        row["hybrid"] = _fuse(row)
    return rows


def main() -> int:
    model = _model()
    for title, rows in (("external", external(model)), ("OXN's own", own(model))):
        print(f"\n{title}, {len(rows)} pairs")
        for label, key in (("BM25", "bm25"), ("model2vec", "semantic"), ("both (RRF)", "hybrid")):
            at_one, at_three, mrr = _score(rows, key)
            print(f"  {label:<12} P@1 {at_one:.3f}  P@3 {at_three:.3f}  MRR {mrr:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
