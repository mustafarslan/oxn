"""The context channel: what an agent is *shown*, as opposed to what the gate accepts.

[ADR-0003](../../../docs/adr/0003-enforcement-model.md) splits OXN in two -- hooks enforce,
MCP informs -- and this package is the second half.
[ADR-0006](../../../docs/adr/0006-retrieval-and-budgeting.md) fixes its one hard rule:

> Retrieval never blocks, and the gate never imports it.

`oxn.check` and `oxn.rules` may not reach anything here, and `tests/test_import_guard.py`
asserts it. A score that can be wrong by degrees must never decide a boolean; anything in
this package that could suppress, reorder or hide a *finding* would quietly re-open
ADR-0002's rule that only an EXACT, fresh measurement may block. The dependency runs one
way, and a bundle is a second artifact computed from the same rules -- never a filter on
the first.
"""
