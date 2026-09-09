#!/usr/bin/env python3
"""Freeze the ADR corpus the retrieval benchmark ranks, and record which commit it is.

`extract_retrieval_labels.py` already argues this for the *queries*: "a test cannot pin a
number that moves underneath it", so the label set is written to a file, stamped with the
HEAD it came from, and regenerated on purpose. **The corpus was never given the same
treatment**, and that asymmetry was the whole defect: half the experiment was frozen and half
was the repository's own ADRs, which every commit edits.

The cost was seventeen re-pins of `tests/test_retrieval_quality.py` in three days, P@1 0.377
-> 0.623, without one line of the ranker changing. ADR-0002 is gold for most of the label
set and roughly tripled in length over those commits, so BM25's term statistics moved under a
fixed query set. Two things follow, and the second is the dangerous one:

* the number looked like retrieval improving and was the gold document getting longer;
* a test that fails on every documentation edit teaches you to re-pin without reading, so a
  real ranker regression would have looked exactly like the seventeen benign ones.

What is frozen is the *document text* BM25 sees -- `decision_documents` output, title, tags
and body -- rather than the ADR files, because that is the input the ranker actually has and
a JSON of it cannot be mistaken for a source of truth the way a mirrored `docs/adr/` tree
could.

    python scripts/freeze_retrieval_corpus.py            # re-freeze at the current HEAD
    python scripts/freeze_retrieval_corpus.py --status   # report whether it would change

`--status` exits 0 and is not a gate, for the same reason its sibling's does not: it says
*stale* from the first ADR edit after a freeze, by design. **Regenerating is not maintenance,
it is moving the benchmark.** Do it when the ranker changes, or when re-baselining on a newer
set of decisions is the explicit intent -- never to make a failing test pass after editing an
ADR, which is the exact reflex this file exists to remove.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "benchmarks" / "retrieval-corpus-oxn.json"
LABELS = ROOT / "benchmarks" / "retrieval-labels-oxn.json"


def head() -> str:
    """The commit this freeze describes. Unknown rather than fatal outside a checkout."""
    try:
        found = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git on PATH
        return "unknown"
    return found.stdout.strip() or "unknown"


def freeze() -> dict[str, object]:
    """The exact text BM25 ranks, for the decisions the label set names."""
    sys.path.insert(0, str(ROOT / "src"))
    from oxn.context.decisions import decision_documents
    from oxn.rules.adr import load_decisions

    wanted = set(json.loads(LABELS.read_text())["decisions"])
    decisions = [decision for decision in load_decisions(ROOT) if decision.identifier in wanted]
    documents = decision_documents(decisions)
    missing = wanted - {document.identifier for document in documents}
    if missing:
        raise SystemExit(f"the label set names decisions that no longer exist: {sorted(missing)}")
    return {
        "frozen_at": head(),
        "rule": (
            "The document text BM25 ranks, frozen so that editing an ADR cannot move a "
            "pinned retrieval number. Regenerate only when the ranker changes or when "
            "re-baselining is the explicit intent."
        ),
        "documents": {document.identifier: document.text for document in documents},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status", action="store_true", help="Report whether the frozen corpus would change."
    )
    args = parser.parse_args()

    fresh = freeze()
    rendered = json.dumps(fresh, indent=2, sort_keys=True) + "\n"
    if args.status:
        current = json.loads(CORPUS.read_text()) if CORPUS.exists() else {}
        stale = current.get("documents") != fresh["documents"]
        print(f"{len(fresh['documents'])} decisions; {'stale' if stale else 'current'}")
        return 0  # never a gate -- see the module docstring
    CORPUS.write_text(rendered)
    print(
        f"wrote {CORPUS.relative_to(ROOT)}: {len(fresh['documents'])} decisions "
        f"at {fresh['frozen_at'][:8]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
