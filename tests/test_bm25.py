"""BM25, tested as arithmetic rather than as vibes.

Each test below names a property of Okapi BM25 that a plausible wrong implementation
satisfies everything else without: IDF falls as a term spreads, TF saturates, length is
normalized, ties are total. A retrieval module that is only tested by "does this query
return the document I had in mind" passes for as long as the author's intuition and the
implementation share a bug.

ADR-0006 section 5 owns the other half -- whether the ranking is any *good* on labelled
task -> ADR pairs. That is a different question from whether it is BM25.
"""

from __future__ import annotations

from oxn.context.bm25 import BM25, Document, tokenize


def _corpus(**documents: str) -> BM25:
    return BM25(Document(identifier, text) for identifier, text in documents.items())


# ---- the tokenizer ----------------------------------------------------------------------


def test_identifiers_are_split_into_their_parts() -> None:
    """A task says "the ADR loader"; the decision says `load_decisions`. Both must tokenize
    to words, or the most discriminative terms in this corpus never match."""
    assert tokenize("load_decisions") == ["load", "decisions"]
    assert tokenize("DependencyGraph.build") == ["dependency", "graph", "build"]
    assert tokenize("applies-to") == ["applies", "to"]


def test_an_acronym_stays_one_token() -> None:
    """Half this corpus is about MCP and SCIP; shredding those to letters loses them."""
    assert tokenize("MCP and SCIP") == ["mcp", "and", "scip"]
    assert tokenize("ADR-0006") == ["adr", "0006"]


# ---- the arithmetic ---------------------------------------------------------------------


def test_a_rarer_term_is_worth_more_than_a_common_one() -> None:
    """IDF. Without it, ranking is term counting and every document about architecture wins
    every query about architecture."""
    index = _corpus(
        a="rare common padding here",
        b="common padding here also",
        c="common padding here again",
        d="common padding here likewise",
    )
    (rare,) = index.rank("rare")
    common = next(hit for hit in index.rank("common") if hit.identifier == "a")
    assert rare.score > common.score


def test_term_frequency_saturates() -> None:
    """Four occurrences are worth more than one and much less than four times one -- which
    is the whole reason BM25 exists rather than raw TF-IDF."""
    index = _corpus(once="alpha beta gamma delta", four="alpha alpha alpha alpha")
    scores = {hit.identifier: hit.score for hit in index.rank("alpha")}
    assert scores["once"] < scores["four"] < 4 * scores["once"]


def test_a_shorter_document_wins_on_the_same_term() -> None:
    """Length normalization at strength `b`. ADRs here range from 4k to 18k characters, so
    without it the longest decision is the most relevant decision for every query."""
    index = _corpus(short="alpha beta", long="alpha " + " ".join(f"w{n}" for n in range(60)))
    assert [hit.identifier for hit in index.rank("alpha")] == ["short", "long"]


# ---- the contract the tests in ADR-0006 section 5 will depend on ------------------------


def test_a_document_sharing_nothing_with_the_query_is_dropped() -> None:
    """Not ranked last -- dropped. A cap of seven filled with four unrelated constraints is
    worse than a bundle of three."""
    index = _corpus(hit="the parser was rewritten", miss="unrelated prose about licensing")
    assert [hit.identifier for hit in index.rank("parser")] == ["hit"]


def test_ties_break_on_the_identifier() -> None:
    """Exact ties are routine on a corpus of six, and a ranking that varies with dictionary
    order cannot have a floor asserted against it."""
    index = _corpus(zebra="identical text", alpha="identical text", magpie="identical text")
    assert [hit.identifier for hit in index.rank("identical")] == ["alpha", "magpie", "zebra"]


def test_an_empty_query_or_an_empty_corpus_ranks_nothing() -> None:
    assert _corpus(a="something").rank("") == []
    assert BM25([]).rank("anything") == []


def test_the_limit_truncates_the_ranking_and_never_reorders_it() -> None:
    index = _corpus(a="alpha", b="alpha beta", c="alpha beta gamma")
    full = index.rank("alpha")
    assert [hit.identifier for hit in index.rank("alpha", limit=2)] == [
        hit.identifier for hit in full[:2]
    ]
