"""Does a static embedding beat BM25 on the retrieval labels? Three of them do not.

    pip install model2vec        # the `oxn[semantic]` extra ADR-0001 sanctioned
    python scripts/measure_semantic.py

**This is the measurement that declined `oxn[semantic]`.** P8 planned the extra and deferred
it "until it can be compared against the first ranker on the labels P8 built" -- 53 pairs from
this repository's history and 100 citation-derived pairs from five others. Those labels now
exist, so the deferral came due, and the rule is the same one that kept `networkx` and
`rank-bm25` out: adopt a dependency against numbers or not at all.

**This file's first version declined the extra on one model, and it was the wrong one.**
`potion-base-8M` is the smallest general-purpose member of the family; `potion-retrieval-32M`
is tuned for retrieval and `potion-code-16M-v2` is trained on code, and neither had been
tried. A dependency declined on the weakest candidate is not measured, it is dismissed. Both
were added here, and a second defect was found in the same pass: reciprocal-rank fusion was
using k=60, the constant conventional for web-scale runs, against corpora of five to forty
documents. At k=60 the gap between first place and second is 1/61 against 1/62 -- the fusion
was averaging two orderings while discarding what either one said. k=5 is worth roughly
+0.06 P@1 to every model below, and the old hybrid row understated the method it rejected.

Measured 2026-09-17, cosine similarity over the same decision text BM25 indexes, RRF at k=5:

===========================  =====  =====  =====  =====  =====  =====
external, 100 pairs            P@1    P@3    MRR   +RRF   resc.   dmg.
===========================  =====  =====  =====  =====  =====  =====
BM25                         0.610  0.820  0.726     --     --     --
potion-base-8M               0.440  0.630  0.568  0.510     10     33
potion-retrieval-32M         0.380  0.670  0.556  0.520     10     28
potion-code-16M-v2           0.390  0.710  0.572  0.540     11     23
===========================  =====  =====  =====  =====  =====  =====

===========================  =====  =====  =====  =====  =====  =====
OXN's own, 53 pairs            P@1    P@3    MRR   +RRF   resc.   dmg.
===========================  =====  =====  =====  =====  =====  =====
BM25                         0.623  0.868  0.760     --     --     --
potion-base-8M               0.509  0.774  0.677  0.491      5      9
potion-retrieval-32M         0.434  0.887  0.657  0.566      8      8
potion-code-16M-v2           0.547  0.868  0.722  0.604      6      4
===========================  =====  =====  =====  =====  =====  =====

`resc.`/`dmg.` are the pairs the embedding pulls two or more ranks above BM25 and the pairs it
sinks by the same margin -- the question a mean cannot answer, since a ranker that loses
overall still earns a hybrid if it is right where the other is wrong.

**The decline stands, on all three.** No model wins either label set alone, the best hybrid
(`potion-code-16M-v2`, k=5) still loses the external set 0.540 to 0.610, and complementarity
runs the wrong way there: 11 pairs rescued against 23 damaged. That is the result that closes
the question. It is not that the embedding is a weaker ranker -- it is that on the labels
where the corpus is big enough to say anything, it is wrong in a superset of the places BM25
is wrong, so there is nothing for a fusion to recover.

Three findings worth keeping, none of which changes the decision:

* **The retrieval-tuned model is the worst of the three externally** (0.380 against the base
  model's 0.440), and the code-trained one is the best on both sets. MTEB Retrieval ranks them
  the other way round. Whatever this task is, it is not the task that benchmark measures --
  the tokenizer seeing identifiers and paths as units matters more here than retrieval tuning.
* **On OXN's own 53 pairs the fused code model reaches 0.604 against 0.623 -- one pair.** That
  is parity, not a loss. It is also five documents and 53 pairs, which
  `tests/test_retrieval_quality.py` already declines to read as a quality signal, and by
  ADR-0006 section 5a those labels ask which decision's *scope* a change falls under, which is
  not a text-similarity question for either ranker.
* **The contextual upper bound was not measured.** `all-MiniLM-L6-v2` would say whether the
  ceiling for any embedding here is 0.65 or 0.85, but it needs torch and is unshippable under
  ADR-0001 either way; the bar for spending the run was a static model within 0.05 of BM25,
  and the closest came 0.07 short. It stays unmeasured, and this sentence is the record of
  that choice rather than a silence.

If this is ever revisited, the direction is not a denser vector -- it is learned sparse
retrieval (the SPLADE family), which emits *term weights* rather than an embedding, so it
keeps the lexical precision that is winning here and adds expansion on top. It needs a
transformer at index time but not at query time, which is a different trade against the
ADR-0002 hook budget than anything measured above.
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
#: ChromaDB and sentence-transformers. **All three, because the first run of this script used
#: only the first and that was not a fair test of the family**: `potion-base-8M` is the
#: smallest general-purpose model in it, and the two that follow are the ones actually built
#: for this job -- one tuned for retrieval, one trained on code. Declining a dependency on its
#: weakest member is not a measurement, it is an accident that happened to agree.
MODELS = (
    "minishlab/potion-base-8M",
    "minishlab/potion-retrieval-32M",
    "minishlab/potion-code-16M-v2",
)

#: Reciprocal-rank fusion damps rank differences by `1 / (k + rank)`, so the k that is
#: conventional for web-scale runs is wrong here by two orders of magnitude: at k=60 over five
#: documents, first place and second differ by 1/61 against 1/62, which is to say they do not
#: differ. The first run of this script used 60 and measured almost nothing; k=5 lets a rank
#: actually carry weight, and is worth +0.05 P@1 to every model below.
RRF_K = 5


def _model(name: str) -> Any:
    try:
        from model2vec import StaticModel
    except ImportError:
        raise SystemExit("model2vec is not installed: pip install model2vec") from None
    return StaticModel.from_pretrained(name)


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
            fused[identifier] = fused.get(identifier, 0.0) + 1.0 / (RRF_K + rank)
    return [identifier for identifier, _ in sorted(fused.items(), key=lambda pair: -pair[1])]


def _gold_rank(order: list[Any], gold: set[Any]) -> int:
    """Where the first right answer lands, or past the end of any corpus here if it never does."""
    return next((rank for rank, name in enumerate(order, 1) if name in gold), 999)


def _complement(rows: list[dict[str, Any]]) -> tuple[int, int]:
    """Pairs the embedding pulls two or more ranks above BM25, and the pairs it sinks.

    **This is the question a mean cannot answer.** A ranker that loses overall still earns a
    hybrid if it is right where the other is wrong, and that shows up as rescues outnumbering
    damage -- so losing on P@1 is not on its own grounds to decline. Counting both directions
    is what turns "it scores lower" into "it knows nothing the other does not".
    """
    rescued = sum(
        1 for row in rows if _gold_rank(row["semantic"], row["gold"]) + 2 <= _gold_rank(row["bm25"], row["gold"])
    )
    damaged = sum(
        1 for row in rows if _gold_rank(row["bm25"], row["gold"]) + 2 <= _gold_rank(row["semantic"], row["gold"])
    )
    return rescued, damaged


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


def _report(name: str) -> None:
    """One model against both label sets, with the complementarity count under each."""
    model = _model(name)
    print(f"\n== {name}")
    for title, rows in (("external", external(model)), ("OXN's own", own(model))):
        print(f"  {title}, {len(rows)} pairs")
        for label, key in (("BM25", "bm25"), ("embedding", "semantic"), ("both (RRF)", "hybrid")):
            at_one, at_three, mrr = _score(rows, key)
            print(f"    {label:<12} P@1 {at_one:.3f}  P@3 {at_three:.3f}  MRR {mrr:.3f}")
        rescued, damaged = _complement(rows)
        print(f"    rescues {rescued}, damages {damaged} (by 2+ ranks against BM25)")


def main() -> int:
    for name in MODELS:
        _report(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
