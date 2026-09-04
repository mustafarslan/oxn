"""Okapi BM25, in the standard library, over a corpus small enough to fit in memory.

Roughly sixty lines of arithmetic, as [ADR-0001](../../../docs/adr/0001-dependency-policy.md)
requires and the ROADMAP estimated: a decision corpus is tens of documents, so the ranking
problem here is nothing like web search and an embedding model would be a network fetch
(ADR-0001 rule 4) bought for a corpus that fits in a dictionary.

Two details are not folklore and are worth stating, because both were chosen against a
plausible alternative:

**The tokenizer splits identifiers.** `load_decisions`, `DependencyGraph` and `applies-to`
all become their parts. Tasks and decisions are both written by programmers about code, so
identifiers are the *most* discriminative terms in this corpus -- and a tokenizer treating
them as opaque strings throws away exactly the matches worth having, because a task says
"the ADR loader" where the decision says `load_decisions`.

**Ties break on the identifier.** BM25 produces exact ties routinely on a corpus this
small, and a ranking that varies with dictionary iteration order cannot be regression
tested. ADR-0006 section 5 makes retrieval quality a test asserting a floor, which requires
the ranking to be a function of the corpus and nothing else.

`k1` and `b` come from `oxn.thresholds` rather than living here as literals -- see
`oxn.calibration` for why every tunable number in OXN is addressable as data.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from oxn import thresholds

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable

#: One token is a word, a number, or one camelCase / snake_case part of an identifier.
#: `[A-Z]+(?![a-z])` keeps acronyms whole (`ADR`, `MCP`) instead of shredding them to
#: letters, which matters when half the corpus is about `MCP` and `SCIP`.
_TOKEN = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercased tokens, identifiers split into their parts."""
    return [match.group(0).lower() for match in _TOKEN.finditer(text)]


@dataclass(frozen=True, slots=True)
class Document:
    """One thing that can be retrieved, reduced to an id and the text that describes it."""

    identifier: str
    text: str


@dataclass(frozen=True, slots=True)
class Hit:
    """One ranked document. `score` is comparable within a query and never across queries."""

    identifier: str
    score: float


class BM25:
    """A ranking index over a fixed corpus. Built once, queried many times.

    Building is O(total tokens) and is *not* hook-path work, which is why nothing in
    `oxn.rules.adr` constructs one: `check.py` loads decisions on every edit.
    """

    __slots__ = ("_average_length", "_b", "_k1", "_lengths", "_postings", "_size")

    def __init__(
        self,
        documents: Iterable[Document],
        *,
        k1: float = thresholds.BM25_K1,
        b: float = thresholds.BM25_B,
    ) -> None:
        self._k1 = k1
        self._b = b
        counted = [
            (document.identifier, Counter(tokenize(document.text))) for document in documents
        ]
        self._lengths = {identifier: sum(terms.values()) for identifier, terms in counted}
        self._size = len(counted)
        self._average_length = sum(self._lengths.values()) / self._size if self._size else 0.0
        self._postings: dict[str, dict[str, int]] = {}
        for identifier, terms in counted:
            for term, frequency in terms.items():
                self._postings.setdefault(term, {})[identifier] = frequency

    def rank(self, query: str, limit: int | None = None) -> list[Hit]:
        """The documents sharing at least one term with `query`, best first.

        A document sharing nothing with the query is *absent*, not ranked last: only
        documents reached through a posting list ever enter `scores`. That is the behaviour
        a caller's cap depends on -- "seven constraints, four of them unrelated" is worse
        than a bundle of three -- so it is asserted rather than assumed.

        There is deliberately no `score > 0` filter here. An earlier draft had one;
        sabotaging it changed no test and could not, because every posting yields a
        positive IDF and a positive saturation term. A guard that cannot fire reads like a
        guarantee and is only decoration.
        """
        scores: dict[str, float] = {}
        for term in tokenize(query):
            postings = self._postings.get(term)
            if postings is None:
                continue
            weight = self._inverse_document_frequency(len(postings))
            for identifier, frequency in postings.items():
                scores[identifier] = scores.get(identifier, 0.0) + weight * self._saturate(
                    frequency, identifier
                )
        ranked = sorted(scores.items(), key=lambda hit: (-hit[1], hit[0]))
        hits = [Hit(identifier, score) for identifier, score in ranked]
        return hits if limit is None else hits[:limit]

    def _inverse_document_frequency(self, document_frequency: int) -> float:
        """Robertson's smoothed IDF. The `1 +` is what keeps it non-negative on a corpus
        where a term appears in more than half the documents -- which, in a corpus of six
        ADRs all discussing architecture, is most of the interesting vocabulary."""
        numerator = self._size - document_frequency + 0.5
        return math.log(1 + numerator / (document_frequency + 0.5))

    def _saturate(self, frequency: int, identifier: str) -> float:
        """Term frequency, saturating at `k1` and normalized by length at strength `b`."""
        relative = self._lengths[identifier] / self._average_length if self._average_length else 1.0
        norm = self._k1 * (1 - self._b + self._b * relative)
        return frequency * (self._k1 + 1) / (frequency + norm)
