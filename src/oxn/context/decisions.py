"""Decisions projected into retrievable documents.

One line of code and a paragraph of reasoning, because *which* text goes in decides what
the measurement in
[ADR-0006](../../../docs/adr/0006-retrieval-and-budgeting.md) section 5 is able to mean.

**`applies-to` is deliberately excluded.** It is the most predictive field in an ADR --
"which decisions govern this file" is answerable from it exactly -- and the git-derived
labels of ADR-0006 section 5 are *made of* it, so a ranker reading it is scored partly
against its own input. Scope belongs to the bundle, which joins on it directly rather than
matching words against it.

Measured rather than assumed, because the honest version of that argument is smaller than
the tidy one: including `applies-to` moves MRR on those labels by +0.002. The queries are
commit subjects and subjects rarely quote paths, so the leak barely opens *for this query
shape*. It opens wide for the shape the MCP server will actually see -- a task that names
the file it is about -- which is why the rule stands on the leak rather than on the size of
it here.

**Filtering is the caller's.** The gate enforces only accepted decisions
([ADR-0005](../../../docs/adr/0005-rule-engine.md)), but a superseded decision is often the
best answer to "why is this forbidden" -- so this projects whatever it is handed and leaves
the choice to the surface that knows what it is for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from oxn.context.bm25 import Document

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable

    from oxn.rules.adr import Decision


def decision_documents(decisions: Iterable[Decision]) -> list[Document]:
    """Title, tags and prose -- everything a task could plausibly be phrased against."""
    return [
        Document(
            decision.identifier,
            f"{decision.title}\n{' '.join(decision.tags)}\n{decision.body}",
        )
        for decision in decisions
    ]
