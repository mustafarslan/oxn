<!-- oxn:begin -->
## Architecture and quality invariants (OXN)

This repository is gated by [OXN](https://github.com/mustafarslan/oxn). A `PostToolUse`
hook runs `oxn check --json` after every edit; a non-zero exit means the edit broke an
invariant, and the JSON names the entity, the rule and the number.

* Ceilings and layer rules live in `oxn.yaml`. Read it before assuming a limit. Layer
  contracts are checked on the edit that breaks them, not only in CI, so an import that
  reaches across a boundary is rejected with the offending edge named.
* A violation is not a suggestion. Fix the cause -- do not split a function into one-line
  helpers to get under a ceiling. That is reported as rule `shredding`, which totals a
  function together with the private, trivial helpers only it calls: dedicated helpers do
  not raise the budget.
* `.oxn/baseline.json` records pre-existing debt. It may not grow: a baselined violation
  that gets worse fails the build exactly as a new one does.
* The loop is bounded. After `retry_budget` failed repairs of the *same* violation, the
  hook stops asking for a fix and says so: stop editing, and report to the user what you
  tried and what the numbers did. Repairing it on a later attempt is fine -- the count is
  per violation, and clearing one forgets it.

Run `oxn check` yourself at any time; `oxn check --deep` adds the architectural tier.

OXN also serves MCP over stdio (`oxn serve`, wired in `.mcp.json`). It informs; it never
blocks. Call `get_architectural_context` *before* writing code to see the constraints that
govern the task, and `explain_violation` on anything the hook rejects -- it gives the
increment trail and where that ceiling was declared.
<!-- oxn:end -->
