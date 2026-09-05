"""The attempt ledger — how many times this session has been told about this violation.

`PostToolUse` fires once per edit and remembers nothing, so a budget over *attempts*
(ADR-0003 section 4) needs state the hook itself cannot hold. This module is that state:
a small JSON file per agent session, keyed by the finding identity `oxn.check.Finding.key`
already guarantees is stable across edits.

Three properties, each of which the obvious implementation gets wrong:

**A fixed violation must stop counting.** The hook checks the one file the agent edited, so
a run says nothing about any other file. Pruning the whole ledger to "what failed just now"
would forget every violation elsewhere in the repository and hand the agent a fresh budget
for each. The ledger is therefore keyed by path first, and only the paths this run actually
measured are rewritten.

**Two hooks can run at once.** Claude Code issues parallel edits, so two processes may write
the same session file. The write is tmp-plus-rename, which is atomic on POSIX: a reader sees
one version or the other, never half of one. Two racing writers can still lose one increment
between them, and that is the right trade -- an under-count costs one extra attempt, while a
lock costs the hook its latency budget on every edit.

**Nothing here may import the rest of OXN.** It is called from the surfaces, and it holds
policy state rather than analysis, so it sits in the `analysis` layer of `oxn.yaml` for the
same reason `context/*` does. Keeping it to the standard library and to plain strings means
the layered contract cannot be violated by it, and the hook fast path pays nothing to load
it.

Session files are pruned by age on write: an agent session is a working day at the outside,
and a directory that only ever grows is a bug report waiting to be filed.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

#: Where ledgers live, under the *cache* rather than beside the baseline. This is derived
#: state about one machine's one session: committing it would share one agent's retry count
#: with everybody, which is the opposite of what a session-scoped budget means.
LEDGER_DIR = Path(".oxn") / "cache" / "attempts"

#: Session files older than this are deleted the next time any ledger is written.
MAX_LEDGER_AGE_S = 7 * 24 * 60 * 60

#: How much of a value trajectory to keep. Enough to show whether the agent was converging
#: when the budget ran out, which is the question the halt report has to answer.
MAX_TRAJECTORY = 8

#: A session id arrives from an external payload and becomes a filename. Anything outside
#: this set is replaced, so no payload can name a path of its own choosing.
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True, slots=True)
class Attempt:
    """How this session has fared against one violation."""

    #: How many times it has been reported, this run included. 1 is the first sighting,
    #: at which point the agent has not yet been given a chance to fix anything.
    count: int
    #: The measured value at each of those reports, oldest first. `27, 19, 19` says the
    #: agent improved once and then stalled; `27, 27, 27` says nothing moved at all.
    values: tuple[float, ...]

    def exhausted(self, budget: int) -> bool:
        """True once the agent has spent `budget` repairs on this and it is still failing.

        Report *n* means *n - 1* repairs were attempted and none worked, so a budget of 3 is
        spent on the fourth sighting rather than the third.
        """
        return budget > 0 and self.count > budget

    @property
    def trend(self) -> str:
        """The trajectory, for a human reading the halt report."""
        return " -> ".join(f"{value:g}" for value in self.values)


def ledger_path(root: Path, session: str) -> Path:
    """Where `session`'s ledger lives under `root`."""
    return root / LEDGER_DIR / f"{_safe(session)}.json"


def charge(path: Path, measured: dict[str, dict[str, float]]) -> dict[str, Attempt]:
    """Record one hook run against the ledger at `path`, and say where each violation stands.

    `measured` maps each path this run looked at to the violations it found there, as
    `{finding key: value}` -- a path with an empty mapping is a file that came back clean,
    which is how a repaired violation is forgotten. Paths absent from `measured` were not
    measured and keep whatever the ledger already holds for them.

    Returns an `Attempt` for every violation in `measured`, the current run included.
    """
    stored = _read(path)
    attempts: dict[str, Attempt] = {}
    for source, findings in measured.items():
        previous = stored.get(source, {})
        current: dict[str, object] = {}
        for key, value in findings.items():
            attempt = _extend(previous.get(key), value)
            attempts[key] = attempt
            current[key] = {"count": attempt.count, "values": list(attempt.values)}
        stored[source] = current
    _write(path, stored)
    return attempts


def _extend(previous: object, value: float) -> Attempt:
    """This violation's history with the current sighting appended."""
    count: int = 0
    values: tuple[float, ...] = ()
    if isinstance(previous, dict):
        raw_count = previous.get("count")
        count = raw_count if isinstance(raw_count, int) and raw_count > 0 else 0
        raw_values = previous.get("values")
        if isinstance(raw_values, list):
            values = tuple(item for item in raw_values if isinstance(item, (int, float)))
    return Attempt(count=count + 1, values=(*values, float(value))[-MAX_TRAJECTORY:])


def _safe(session: str) -> str:
    """A session id reduced to something that can only ever be one filename.

    Truncated as well as substituted: a payload is free to send a kilobyte, and every
    filesystem OXN runs on has a name-length limit that a hook must not be able to trip.
    """
    return _UNSAFE.sub("_", session)[:96] or "unnamed"


def _read(path: Path) -> dict[str, dict[str, object]]:
    """The ledger, or an empty one. A corrupt file is discarded rather than raised at a hook.

    Losing a retry count costs an agent one extra attempt. Failing the hook because a JSON
    file was truncated by a crash costs the gate itself, which is a far worse trade.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    paths = raw.get("paths") if isinstance(raw, dict) else None
    if not isinstance(paths, dict):
        return {}
    return {source: entries for source, entries in paths.items() if isinstance(entries, dict)}


def _write(path: Path, paths: dict[str, dict[str, object]]) -> None:
    """Replace the ledger atomically, and never fail the hook if that cannot be done."""
    payload = json.dumps({"updated": int(time.time()), "paths": paths}, indent=2) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _prune(path.parent)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(payload)
        os.replace(temporary, path)
    except OSError:
        return


def _prune(directory: Path, now: float | None = None) -> None:
    """Delete ledgers older than `MAX_LEDGER_AGE_S`. Best effort, by design."""
    cutoff = (now if now is not None else time.time()) - MAX_LEDGER_AGE_S
    for entry in directory.glob("*.json"):
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:  # pragma: no cover - a file another process just removed
            continue
