#!/usr/bin/env python3
"""Derive task -> ADR labels from *citations*, on repositories that are not OXN.

`extract_retrieval_labels.py` derives OXN's own labels from `applies-to`. That does not
transfer: `applies-to` is an OXN convention, and no third-party ADR has one. So the label
here is a different observation, on someone else's project:

    task  = a commit that cites an ADR while doing the work it governs
    gold  = the ADR it cited

Four filters, each of which throws away real pairs and is worth it:

* **The commit must not be authoring the decision.** "Add ADR-25", "Reword ADR-25" -- the
  subject *is* the ADR's title, so the pair is a paraphrase with the paraphrase left in.
  This is most citations in most repositories, which is the survey's first finding.
* **The cited ADR must exist at the pinned commit.** A citation of a decision since
  deleted or renumbered is a label pointing at nothing.
* **Something must survive stripping the citation** -- three content words, after removing
  the ADR reference, the conventional-commit prefix, PR and issue numbers, and trailers.
  "Implement ADR-0012" leaves nothing; it is a paraphrase pair, not a task.
* **Leakage is measured, not assumed.** A pair whose text repeats 60% or more of the cited
  decision's *title* is flagged `restates_title`. Those are kept in the file and reported
  separately, because "the query already contains the answer" is a property to quantify
  rather than a reason to quietly drop rows.

`agent_authored` is recorded per pair, not filtered. Half of every usable pair found in
open source carries `Co-Authored-By: Claude`, which is a fact about where this kind of data
exists rather than a defect -- and OXN governs agents, so it is arguably the population.
Results are reported with and without.

**No task text is stored.** The file holds `(corpus, sha, gold)`, and the evaluation reads
the subject and body from the pinned clone. Task text is other people's prose, and this
repository's rule is to pin corpora rather than vendor them.

    python scripts/fetch_corpora.py --use retrieval    # first: fetch what this reads
    python scripts/extract_citation_labels.py
    python scripts/extract_citation_labels.py --status
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPORA = ROOT / "benchmarks" / "corpora"
LABELS = ROOT / "benchmarks" / "retrieval-labels-external.json"

sys.path.insert(0, str(ROOT / "src"))

#: `ADR-0009`, `ADR 9`, `adr_14`. The number is what identifies the decision.
CITATION = re.compile(r"\bADR[- _]?(\d{1,4})\b", re.I)

#: A subject about the decision *document*. Deliberately generous: a false positive costs
#: one pair, a false negative admits a paraphrase and inflates the result.
AUTHORING = re.compile(
    r"\b(add|accept|record|refine|reword|mark|supersede[sd]?|amend|draft|propose|create"
    r"|write|rename)\b[^.]{0,40}\badr\b|^docs?[:(]|\badr[- _]?\d+\s*:",
    re.I,
)

#: Conventional-commit prefixes, PR and issue numbers, ticket ids, and git trailers -- all
#: of them noise that is not the task.
NOISE = re.compile(
    r"^\w+(\([^)]*\))?!?:|\(#\d+\)|#\d+|\b[A-Z]{2,}-\d+\b"
    r"|^\s*(Co-Authored-By|Closes|Fixes|Refs|Signed-off-by):.*$|https?://\S+",
    re.M | re.I,
)

WORD = re.compile(r"[A-Za-z]{3,}")
STOP = frozenset(
    ["the", "and", "for", "with", "from", "into", "that", "this", "per", "see", "ref", "adr"]
)

#: Share of the cited decision's title that the task text repeats before the pair is
#: flagged. 0.6 is a judgement, and the flag is reported rather than acted on.
RESTATES_TITLE = 0.6


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True).stdout


def words(text: str) -> set[str]:
    return {w.lower() for w in WORD.findall(text) if w.lower() not in STOP}


def titles(root: Path, adr_dir: str) -> dict[int, str]:
    """Every decision at the pinned commit, by its number. Read through OXN's own parser."""
    from oxn.rules.adr import load_decisions

    found = {}
    for decision in load_decisions(root, Path(adr_dir)):
        digits = re.sub(r"\D", "", decision.identifier)
        if digits:
            found[int(digits)] = decision.title
    return found


def task_text(subject: str, body: str) -> str:
    """What a retriever would be given: the commit, minus the answer and the plumbing."""
    return NOISE.sub(" ", CITATION.sub(" ", f"{subject}\n{body}")).strip()


def pairs_in(root: Path, adr_dir: str) -> list[dict[str, object]]:
    """Every usable (commit, decisions) pair in one repository."""
    known = titles(root, adr_dir)
    log = git(root, "log", "--no-merges", "--format=\x1e%H\x1f%s\x1f%b")
    found = []
    for block in log.split("\x1e")[1:]:
        parts = block.split("\x1f")
        if len(parts) < 3:
            continue
        pair = _pair(parts[0], parts[1].strip(), parts[2], known)
        if pair is not None:
            found.append(pair)
    return found


def _pair(sha: str, subject: str, body: str, known: dict[int, str]) -> dict[str, object] | None:
    cited = {int(m) for m in CITATION.findall(f"{subject} {body}")}
    gold = sorted(cited & set(known))
    if not gold or AUTHORING.search(subject):
        return None
    text = words(task_text(subject, body))
    if len(text) < 3:
        return None
    repeated = max(len(words(known[g]) & text) / max(1, len(words(known[g]))) for g in gold)
    return {
        "sha": sha,
        "gold": gold,
        "restates_title": repeated >= RESTATES_TITLE,
        "agent_authored": "Co-Authored-By: Claude" in body or "claude.ai/code" in body,
    }


def retrieval_corpora() -> list[dict[str, str]]:
    import yaml

    manifest = yaml.safe_load((ROOT / "benchmarks" / "manifest.yaml").read_text())
    return [entry for entry in manifest["corpora"] if entry.get("use") == "retrieval"]


def extract() -> dict[str, object]:
    corpora = {}
    for entry in retrieval_corpora():
        root = CORPORA / entry["name"]
        if not (root / ".git").exists():
            print(f"  skipped  {entry['name']} (not fetched)")
            continue
        found = pairs_in(root, entry["adr_dir"])
        corpora[entry["name"]] = {
            "sha": git(root, "rev-parse", "HEAD").strip(),
            "adr_dir": entry["adr_dir"],
            "decisions": len(titles(root, entry["adr_dir"])),
            "pairs": found,
        }
        agent = sum(1 for p in found if p["agent_authored"])
        print(f"  {entry['name']:24s} {len(found):3d} pairs, {agent:3d} agent-authored")
    return {
        "rule": (
            "task = a commit citing an ADR while doing the work it governs, minus the "
            "citation; gold = the cited decision. Authoring commits, citations of "
            "decisions absent at the pinned commit, and commits with nothing left after "
            "stripping are excluded. Task text is NOT stored: read it from the pinned clone."
        ),
        "corpora": corpora,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status", action="store_true", help="Report whether the frozen set would change."
    )
    args = parser.parse_args()

    fresh = extract()
    if not fresh["corpora"]:
        print("no retrieval corpora fetched; run scripts/fetch_corpora.py --use retrieval")
        return 1
    if args.status:
        current = json.loads(LABELS.read_text()) if LABELS.exists() else {}
        same = current.get("corpora") == fresh["corpora"]
        print("current" if same else "would change")
        return 0
    LABELS.write_text(json.dumps(fresh, indent=2) + "\n")
    total = sum(len(c["pairs"]) for c in fresh["corpora"].values())
    print(f"wrote {LABELS.relative_to(ROOT)}: {total} pairs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
