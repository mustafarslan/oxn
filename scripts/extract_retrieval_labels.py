#!/usr/bin/env python3
"""Derive task -> ADR labels from git history, and freeze them.

ADR-0006 section 5: a label written by the ranker's author, by reading an ADR title and
paraphrasing it into a task, measures paraphrase rather than retrieval. Git supplies labels
nobody hand-wrote:

    task  = a commit subject -- what somebody actually set out to do
    gold  = the decisions whose `applies-to` covers a file that commit changed

Two rules, both of which cost pairs and are worth the cost:

* **A decision scoped `**` is excluded.** ADR-0001 governs every file in this repository,
  so it is correct for every commit and discriminates nothing. A label set in which one
  answer is always right measures nothing.
* **A commit with no covering decision is dropped**, rather than labelled empty. "No ADR
  governs this" is a fact about scope, not a retrieval target.

The output is *frozen* deliberately. Every commit changes the label set, and a test cannot
pin a number that moves underneath it -- so this writes a file, records the HEAD it was
extracted at, and is re-run on purpose rather than on every test run.

    python scripts/extract_retrieval_labels.py            # rewrite the label set
    python scripts/extract_retrieval_labels.py --check    # fail if it would change

Label *extraction* is data generation and lives here beside `fetch_corpora.py`. The
*measurement* is `tests/test_retrieval_quality.py`, because a number that lives in a script
drifts silently while one that lives in a test fails the build.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABELS = ROOT / "benchmarks" / "retrieval-labels-oxn.json"

sys.path.insert(0, str(ROOT / "src"))


def extract() -> dict[str, object]:
    from oxn.rules.adr import load_decisions
    from oxn.vcs.log import read_log

    decisions = [d for d in load_decisions(ROOT) if "**" not in d.applies_to]
    history = read_log(ROOT)
    pairs = []
    for commit in history.commits:
        gold = sorted(
            decision.identifier
            for decision in decisions
            if _covers(decision.applies_to, [change.path for change in commit.changes])
        )
        if gold:
            pairs.append({"sha": commit.sha, "task": commit.subject, "gold": gold})
    return {
        "extracted_at": _head(),
        "rule": (
            "task = commit subject; gold = decisions whose applies-to covers a changed file. "
            "Decisions scoped `**` are excluded: one that governs everything discriminates "
            "nothing. Commits with no covering decision are dropped."
        ),
        "decisions": sorted(decision.identifier for decision in decisions),
        "pairs": list(reversed(pairs)),
    }


def _covers(patterns: tuple[str, ...], paths: list[str]) -> bool:
    return any(fnmatch(path, pattern) for path in paths for pattern in patterns)


def _head() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the label set is stale.")
    args = parser.parse_args()

    fresh = extract()
    rendered = json.dumps(fresh, indent=2) + "\n"
    if args.check:
        current = LABELS.read_text() if LABELS.exists() else ""
        stale = json.loads(current or "{}").get("pairs") != fresh["pairs"]
        print(f"{len(fresh['pairs'])} pairs; {'stale' if stale else 'current'}")
        return 1 if stale else 0
    LABELS.write_text(rendered)
    where = LABELS.relative_to(ROOT)
    print(f"wrote {where}: {len(fresh['pairs'])} pairs at {fresh['extracted_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
