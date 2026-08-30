"""Type-1 and Type-2 clone detection over normalized token streams.

The approach PMD's CPD uses, and Baker's before it ("On finding duplication and
near-duplication in large software systems", *WCRE 1995*; survey: Roy, Cordy & Koschke,
*SCP* 74(7), 2009): normalize the token stream so renaming cannot hide a copy, then find
repeated windows with a rolling hash and verify each candidate exactly.

**Type-3 (gapped) clones are not detected**, and that limitation is stated rather than
hidden -- a clone report that silently misses a category is worse than one that says what
it covers.

Incremental by construction: window hashes are a pure function of one file, so they cache
alongside everything else and cross-file matching is a report-path join over cached hashes.
Clone detection never runs on the hook path.
"""

from __future__ import annotations

import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from oxn.thresholds import DUPLICATION_MIN_LINES, DUPLICATION_MIN_TOKENS
from oxn.volume.tokens import Token, normalized_tokens

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping

    from tree_sitter import Node

    from oxn.profiles.base import LanguageProfile

#: Polynomial rolling hash over 64 bits. The base is a large odd prime; the mask keeps
#: Python's arbitrary-precision integers to machine width so the arithmetic stays cheap.
_BASE = 1_000_003
_MASK = (1 << 64) - 1


@dataclass(frozen=True, slots=True)
class Clone:
    """One occurrence of duplicated code."""

    path: str
    start_line: int
    end_line: int
    start_token: int
    token_count: int

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1

    @property
    def lines(self) -> frozenset[int]:
        return frozenset(range(self.start_line, self.end_line + 1))


@dataclass(frozen=True, slots=True)
class CloneClass:
    """A group of occurrences that are copies of one another."""

    occurrences: tuple[Clone, ...]

    @property
    def token_count(self) -> int:
        return self.occurrences[0].token_count

    @property
    def duplicate_tokens(self) -> int:
        """Tokens counted as duplication: every occurrence after the first."""
        return self.token_count * (len(self.occurrences) - 1)


@dataclass
class DuplicationReport:
    """Clone classes across an analyzed set, and the verbosity they imply."""

    clone_classes: list[CloneClass] = field(default_factory=list)
    total_tokens: int = 0

    @property
    def duplicate_tokens(self) -> int:
        return sum(klass.duplicate_tokens for klass in self.clone_classes)

    @property
    def duplication_ratio(self) -> float:
        """Fraction of the token stream that is a copy of something else.

        This is the *verbosity* signal: SlopCodeBench measures agent code at 2.2x the
        redundancy of comparable human projects.
        """
        return self.duplicate_tokens / self.total_tokens if self.total_tokens else 0.0

    def duplicate_lines_by_file(self) -> dict[str, set[int]]:
        """Lines participating in any clone, per file, for per-file attribution."""
        out: dict[str, set[int]] = defaultdict(set)
        for klass in self.clone_classes:
            for occurrence in klass.occurrences:
                out[occurrence.path].update(occurrence.lines)
        return dict(out)


def file_windows(
    path: str,
    node: Node,
    profile: LanguageProfile,
    *,
    min_tokens: int = DUPLICATION_MIN_TOKENS,
) -> tuple[list[Token], dict[int, list[int]]]:
    """Token stream plus ``hash -> [start index]`` for every window of ``min_tokens``.

    Returned separately from matching so the hashes can be cached per file and joined
    across files later, which is what keeps this off the hook path.
    """
    tokens = normalized_tokens(node, profile)
    buckets: dict[int, list[int]] = defaultdict(list)
    if len(tokens) < min_tokens:
        return tokens, {}

    # `hash()` is salted per process, so window hashes would differ between runs and the
    # cache would never hit. CRC32 is stable, fast, and collisions are verified away anyway.
    values = [zlib.crc32(token.normalized.encode()) for token in tokens]
    highest = pow(_BASE, min_tokens - 1, 1 << 64)

    rolling = 0
    for value in values[:min_tokens]:
        rolling = (rolling * _BASE + value) & _MASK
    buckets[rolling].append(0)

    for index in range(1, len(values) - min_tokens + 1):
        outgoing = (values[index - 1] * highest) & _MASK
        rolling = ((rolling - outgoing) * _BASE + values[index + min_tokens - 1]) & _MASK
        buckets[rolling].append(index)

    return tokens, dict(buckets)


def find_clones(
    files: Mapping[str, tuple[list[Token], dict[int, list[int]]]],
    *,
    min_tokens: int = DUPLICATION_MIN_TOKENS,
    min_lines: int = DUPLICATION_MIN_LINES,
) -> DuplicationReport:
    """Match windows across an analyzed set into maximal, non-overlapping clone classes."""
    report = DuplicationReport(total_tokens=sum(len(tokens) for tokens, _ in files.values()))

    candidates: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for path, (_, buckets) in files.items():
        for digest, positions in buckets.items():
            for position in positions:
                candidates[digest].append((path, position))

    # Extend every candidate *before* selecting, then take the longest first. Selecting in
    # bucket order instead lets a short clone claim tokens that a longer one needed, and the
    # longer clone -- the one worth reporting -- is then discarded as overlapping.
    extended: list[tuple[int, list[tuple[str, int]]]] = []
    seen_groups: set[tuple[tuple[str, int], ...]] = set()

    for digest in candidates:
        seeds = candidates[digest]
        if len(seeds) < 2:
            continue

        # A hash match is only a candidate. Verify by comparing the tokens themselves --
        # a collision must never be observable in the output.
        reference = _window(files, seeds[0], min_tokens)
        verified = [seed for seed in seeds if _window(files, seed, min_tokens) == reference]
        if len(verified) < 2:
            continue

        grown = _extend(files, verified, min_tokens)
        if grown is None:
            continue
        length, starts = grown
        key = tuple(sorted(starts))
        if key in seen_groups:
            continue
        seen_groups.add(key)
        extended.append((length, starts))

    extended.sort(key=lambda item: (-item[0], -len(item[1])))

    claimed: dict[str, set[int]] = defaultdict(set)
    classes: list[CloneClass] = []

    for length, starts in extended:
        if any(_overlaps(claimed[path], start, length) for path, start in starts):
            continue

        occurrences = tuple(_clone(files, path, start, length) for path, start in starts)
        if any(clone.line_count < min_lines for clone in occurrences):
            continue

        for path, start in starts:
            claimed[path].update(range(start, start + length))
        classes.append(CloneClass(occurrences))

    report.clone_classes = classes
    return report


def _window(
    files: Mapping[str, tuple[list[Token], dict[int, list[int]]]],
    seed: tuple[str, int],
    length: int,
) -> tuple[str, ...]:
    path, start = seed
    tokens, _ = files[path]
    return tuple(token.normalized for token in tokens[start : start + length])


def _extend(
    files: Mapping[str, tuple[list[Token], dict[int, list[int]]]],
    seeds: list[tuple[str, int]],
    min_tokens: int,
) -> tuple[int, list[tuple[str, int]]] | None:
    """Grow a verified match maximally in both directions, without self-overlap.

    The cap matters more than it looks. Two copies of a function in one file are separated
    by a fixed gap, and after normalization the *next* function's header (``def $ID (``)
    often matches too -- so unbounded forward extension runs straight past the real end of
    the clone and the occurrences start overlapping each other. Capping the length at the
    smallest gap keeps the match maximal *and* well-formed; rejecting the overlap after the
    fact instead loses the clone entirely.
    """
    starts = list(seeds)
    length = min_tokens
    limit = _self_overlap_limit(starts)
    if limit is not None and limit < min_tokens:
        return None

    while True:
        previous = [(path, start - 1) for path, start in starts]
        if any(start < 0 for _, start in previous):
            break
        if limit is not None and length + 1 > limit:
            break
        values = {_token_at(files, path, start) for path, start in previous}
        if len(values) != 1 or None in values:
            break
        starts, length = previous, length + 1

    while limit is None or length < limit:
        values = {_token_at(files, path, start + length) for path, start in starts}
        if len(values) != 1 or None in values:
            break
        length += 1

    return length, starts


def _self_overlap_limit(starts: list[tuple[str, int]]) -> int | None:
    """Largest length at which no two occurrences in the same file overlap."""
    by_file: dict[str, list[int]] = defaultdict(list)
    for path, start in starts:
        by_file[path].append(start)

    gaps = [
        later - earlier
        for positions in by_file.values()
        for earlier, later in zip(sorted(positions), sorted(positions)[1:], strict=False)
    ]
    return min(gaps) if gaps else None


def _token_at(
    files: Mapping[str, tuple[list[Token], dict[int, list[int]]]], path: str, index: int
) -> str | None:
    tokens, _ = files[path]
    if index < 0 or index >= len(tokens):
        return None
    return tokens[index].normalized


def _overlaps(claimed: set[int], start: int, length: int) -> bool:
    return any(index in claimed for index in range(start, start + length))


def _clone(
    files: Mapping[str, tuple[list[Token], dict[int, list[int]]]],
    path: str,
    start: int,
    length: int,
) -> Clone:
    tokens, _ = files[path]
    span = tokens[start : start + length]
    return Clone(
        path=path,
        start_line=span[0].line,
        end_line=span[-1].line,
        start_token=start,
        token_count=length,
    )
