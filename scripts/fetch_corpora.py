#!/usr/bin/env python3
"""Fetch the benchmark corpora declared in ``benchmarks/manifest.yaml``.

Corpora are *pinned, not vendored*: this clones each source at a recorded commit into
``benchmarks/corpora/`` (gitignored) and writes the resolved SHAs to
``benchmarks/lock.json`` so an evaluation run can be reproduced exactly.

    python scripts/fetch_corpora.py --list
    python scripts/fetch_corpora.py --use fixture --use threshold
    python scripts/fetch_corpora.py --resolve-only # pin every ref without downloading
    python scripts/fetch_corpora.py --pin          # re-resolve refs and rewrite the lock

Deliberately stdlib-only apart from PyYAML (already a runtime dependency), and it shells
out to git rather than taking a git library -- ADR-0001.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "benchmarks" / "manifest.yaml"
LOCK = ROOT / "benchmarks" / "lock.json"
DEST = ROOT / "benchmarks" / "corpora"


def load_manifest() -> list[dict[str, Any]]:
    import yaml

    data = yaml.safe_load(MANIFEST.read_text())
    return list(data["corpora"])


def load_lock() -> dict[str, str]:
    return json.loads(LOCK.read_text()) if LOCK.exists() else {}


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def resolve(url: str, ref: str) -> str:
    """Resolve a ref to a commit SHA without cloning."""
    out = git("ls-remote", url, ref)
    if not out:
        raise SystemExit(f"could not resolve {ref!r} in {url}")
    return out.split()[0]


def fetch(entry: dict[str, Any], sha: str) -> Path:
    """Shallow-clone one corpus at an exact SHA."""
    target = DEST / entry["name"]
    if (target / ".git").exists():
        current = git("rev-parse", "HEAD", cwd=target)
        if current == sha:
            print(f"  ok       {entry['name']} @ {sha[:12]}")
            return target
        git("fetch", "--depth", "1", "origin", sha, cwd=target)
        git("checkout", "--detach", sha, cwd=target)
        print(f"  updated  {entry['name']} @ {sha[:12]}")
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    git("init", "--quiet", str(target))
    git("remote", "add", "origin", entry["url"], cwd=target)
    git("fetch", "--depth", "1", "origin", sha, cwd=target)
    git("checkout", "--detach", sha, cwd=target)
    print(f"  cloned   {entry['name']} @ {sha[:12]}")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list corpora and exit")
    parser.add_argument("--use", action="append", default=[], help="only this use (repeatable)")
    parser.add_argument("--name", action="append", default=[], help="only this corpus (repeatable)")
    parser.add_argument("--pin", action="store_true", help="re-resolve refs and rewrite the lock")
    parser.add_argument(
        "--resolve-only",
        action="store_true",
        help="record commit SHAs in the lock without downloading anything",
    )
    args = parser.parse_args(argv)

    entries = load_manifest()
    if args.use:
        entries = [e for e in entries if e.get("use") in set(args.use)]
    if args.name:
        entries = [e for e in entries if e["name"] in set(args.name)]

    if args.list:
        for e in entries:
            lang = f" [{e['language']}]" if "language" in e else ""
            print(f"{e['name']:28} {e['use']:10}{lang:14} {e['url']}")
        return 0

    if not entries:
        print("no corpora matched", file=sys.stderr)
        return 1

    lock = load_lock()
    for entry in entries:
        name = entry["name"]
        if args.pin or name not in lock:
            print(f"  resolve  {name} ({entry['ref']}) ...")
            lock[name] = resolve(entry["url"], entry["ref"])
        if args.resolve_only:
            print(f"  pinned   {name} @ {lock[name][:12]}")
        else:
            fetch(entry, lock[name])

    LOCK.write_text(json.dumps(dict(sorted(lock.items())), indent=2) + "\n")
    print(f"\nlock written: {LOCK.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
