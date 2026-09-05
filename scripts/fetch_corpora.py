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


def _fetch_args(entry: dict[str, Any]) -> list[str]:
    """How much history this corpus needs.

    `--depth 1` is right for every corpus measured at a *state*: the metric tiers read the
    tree, never the log. It is wrong for `use: retrieval`, where a label **is** a commit --
    a shallow clone contains none of the commits the labels name, so the lane would skip
    with a corpus sitting on disk. Those clone every commit and no blobs; the checkout then
    pulls only the trees and files it actually needs.
    """
    if entry.get("history") == "full":
        return ["fetch", "--filter=blob:none", "origin"]
    return ["fetch", "--depth", "1", "origin"]


def fetch(entry: dict[str, Any], sha: str) -> Path:
    """Clone one corpus at an exact SHA, with as little history as it can do its job with."""
    target = DEST / entry["name"]
    fetch_args = _fetch_args(entry)
    if (target / ".git").exists():
        current = git("rev-parse", "HEAD", cwd=target)
        if current == sha:
            print(f"  ok       {entry['name']} @ {sha[:12]}")
            return target
        git(*fetch_args, sha, cwd=target)
        git("checkout", "--detach", sha, cwd=target)
        print(f"  updated  {entry['name']} @ {sha[:12]}")
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    git("init", "--quiet", str(target))
    git("remote", "add", "origin", entry["url"], cwd=target)
    git(*fetch_args, sha, cwd=target)
    git("checkout", "--detach", sha, cwd=target)
    print(f"  cloned   {entry['name']} @ {sha[:12]}")
    return target


def _parser() -> argparse.ArgumentParser:
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
    return parser


def _selected(args: argparse.Namespace) -> list[dict]:
    """The manifest narrowed by `--use` and `--name`, both repeatable and both optional."""
    entries = load_manifest()
    if args.use:
        entries = [entry for entry in entries if entry.get("use") in set(args.use)]
    if args.name:
        entries = [entry for entry in entries if entry["name"] in set(args.name)]
    return entries


def _print_list(entries: list[dict]) -> None:
    for entry in entries:
        language = f" [{entry['language']}]" if "language" in entry else ""
        print(f"{entry['name']:28} {entry['use']:10}{language:14} {entry['url']}")


def _acquire(entry: dict, lock: dict[str, str], *, pin: bool, resolve_only: bool) -> None:
    """Pin one corpus to a commit, and fetch it unless only the pin was asked for.

    The lock is what makes a corpus a *fixture*: a benchmark measured against a moving
    branch is not reproducible, and a number quoted from one is not a measurement.
    """
    name = entry["name"]
    if pin or name not in lock:
        print(f"  resolve  {name} ({entry['ref']}) ...")
        lock[name] = resolve(entry["url"], entry["ref"])
    if resolve_only:
        print(f"  pinned   {name} @ {lock[name][:12]}")
    else:
        fetch(entry, lock[name])


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    entries = _selected(args)

    if args.list:
        _print_list(entries)
        return 0
    if not entries:
        print("no corpora matched", file=sys.stderr)
        return 1

    lock = load_lock()
    for entry in entries:
        _acquire(entry, lock, pin=args.pin, resolve_only=args.resolve_only)

    LOCK.write_text(json.dumps(dict(sorted(lock.items())), indent=2) + "\n")
    print(f"\nlock written: {LOCK.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
