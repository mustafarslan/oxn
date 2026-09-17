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

    #: What one analysed file contributes: its token stream, and its rolling-hash buckets.
    #: Type-checking only -- `from __future__ import annotations` makes every signature a
    #: string, and `Mapping` is not imported at runtime.
    Analysed = Mapping[str, tuple[list[Token], dict[int, list[int]]]]

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
    files: Analysed,
    *,
    min_tokens: int = DUPLICATION_MIN_TOKENS,
    min_lines: int = DUPLICATION_MIN_LINES,
) -> DuplicationReport:
    """Match windows across an analyzed set into maximal, non-overlapping clone classes.

    Three phases, and the order of the middle two is the whole trick: every candidate is
    grown to its full length *before* any is selected. Selecting in bucket order instead
    lets a short clone claim tokens a longer one needed, and the longer clone -- the one
    actually worth reporting -- is then discarded as overlapping.
    """
    report = DuplicationReport(total_tokens=sum(len(tokens) for tokens, _ in files.values()))
    grown = _grow_candidates(files, _seed_index(files), min_tokens)
    report.clone_classes = _select(files, grown, min_lines)
    return report


def _seed_index(files: Analysed) -> dict[int, list[tuple[str, int]]]:
    """Rolling-hash digest -> every (path, position) that produced it."""
    candidates: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for path, (_, buckets) in files.items():
        for digest, positions in buckets.items():
            for position in positions:
                candidates[digest].append((path, position))
    return candidates


def _grow_candidates(
    files: Analysed, candidates: dict[int, list[tuple[str, int]]], min_tokens: int
) -> list[tuple[int, list[tuple[str, int]]]]:
    """Verify each hash bucket and extend it, dropping duplicates of the same group."""
    grown: list[tuple[int, list[tuple[str, int]]]] = []
    seen_groups: set[tuple[tuple[str, int], ...]] = set()

    for seeds in candidates.values():
        if len(seeds) < 2:
            continue
        verified = _verify(files, seeds, min_tokens)
        if verified is None:
            continue
        extended = _extend(files, verified, min_tokens)
        if extended is None:
            continue
        key = tuple(sorted(extended[1]))
        if key in seen_groups:
            continue
        seen_groups.add(key)
        grown.append(extended)

    grown.sort(key=lambda item: (-item[0], -len(item[1])))
    return grown


def _verify(
    files: Analysed, seeds: list[tuple[str, int]], min_tokens: int
) -> list[tuple[str, int]] | None:
    """A hash match is only a candidate.

    The tokens themselves are compared, so a collision is never observable in the output.
    """
    reference = _window(files, seeds[0], min_tokens)
    verified = [seed for seed in seeds if _window(files, seed, min_tokens) == reference]
    return verified if len(verified) >= 2 else None


def _select(
    files: Analysed, grown: list[tuple[int, list[tuple[str, int]]]], min_lines: int
) -> list[CloneClass]:
    """Longest first, taking only classes whose tokens are all still unclaimed."""
    claimed: dict[str, set[int]] = defaultdict(set)
    classes: list[CloneClass] = []

    for length, starts in grown:
        free = [seed for seed in starts if not _overlaps(claimed[seed[0]], seed[1], length)]
        occurrences = _admit(files, free, length, min_lines)
        if occurrences is None:
            continue
        for path, start in free:
            claimed[path].update(range(start, start + length))
        classes.append(CloneClass(occurrences))
    return classes


def _admit(
    files: Analysed,
    free: list[tuple[str, int]],
    length: int,
    min_lines: int,
) -> tuple[Clone, ...] | None:
    """The occurrences of this class that may still be reported, or None if too few remain.

    **A class is thinned by what a longer clone already claimed, not refused because of it.**
    Refusing wholesale was the second half of the bug this module's docstring describes fixing
    once: ordering candidates by length stopped a short clone from stealing a long one's
    tokens, but a class whose *one* overlapping occurrence sank its other two was still
    losing real duplication. Measured on spring-petclinic, 14 of 94 candidate classes had two
    or more untouched occurrences and were discarded for the sake of a third.

    Two occurrences is what makes duplication duplication, so that is the bar to clear here;
    spanning fewer than `min_lines` is the other refusal, and it is about a reader's
    attention rather than overlap.
    """
    if len(free) < 2:
        return None
    occurrences = tuple(_clone(files, path, start, length) for path, start in free)
    if any(clone.line_count < min_lines for clone in occurrences):
        return None
    return occurrences


def _window(
    files: Mapping[str, tuple[list[Token], dict[int, list[int]]]],
    seed: tuple[str, int],
    length: int,
) -> tuple[str, ...]:
    path, start = seed
    tokens, _ = files[path]
    return tuple(token.normalized for token in tokens[start : start + length])


def _extend(
    files: Analysed, seeds: list[tuple[str, int]], min_tokens: int
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
    limit = _self_overlap_limit(starts)
    if limit is not None and limit < min_tokens:
        return None

    starts, length = _grow_backward(files, starts, min_tokens, limit)
    return _grow_forward(files, starts, length, limit), starts


def _grow_backward(
    files: Analysed, starts: list[tuple[str, int]], length: int, limit: int | None
) -> tuple[list[tuple[str, int]], int]:
    """Walk every occurrence one token earlier for as long as they still agree."""
    while _room_before(starts, length, limit):
        previous = [(path, start - 1) for path, start in starts]
        values = {_token_at(files, path, start) for path, start in previous}
        if len(values) != 1 or None in values:
            break
        starts, length = previous, length + 1
    return starts, length


def _room_before(starts: list[tuple[str, int]], length: int, limit: int | None) -> bool:
    """Is there a token before every occurrence, and room for it under the overlap cap?"""
    if any(start == 0 for _, start in starts):
        return False
    return limit is None or length + 1 <= limit


def _grow_forward(
    files: Analysed, starts: list[tuple[str, int]], length: int, limit: int | None
) -> int:
    """Extend the tail while every occurrence still holds the same token."""
    while limit is None or length < limit:
        values = {_token_at(files, path, start + length) for path, start in starts}
        if len(values) != 1 or None in values:
            break
        length += 1
    return length


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
