<!-- oxn:begin -->
## Architecture and quality invariants (OXN)

This repository is gated by [OXN](https://github.com/mustafarslan/oxn). A `PostToolUse`
hook runs `oxn check --json` after every edit; a non-zero exit means the edit broke an
invariant, and the JSON names the entity, the rule and the number.

* Ceilings and layer rules live in `oxn.yaml`. Read it before assuming a limit.
* A violation is not a suggestion. Fix the cause -- do not split a function into one-line
  helpers to get under a ceiling. That is reported as rule `shredding`, which totals a
  function together with the private, trivial helpers only it calls: dedicated helpers do
  not raise the budget.
* `.oxn/baseline.json` records pre-existing debt. It may not grow: a baselined violation
  that gets worse fails the build exactly as a new one does.

Run `oxn check` yourself at any time; `oxn check --deep` adds the architectural tier.
<!-- oxn:end -->
