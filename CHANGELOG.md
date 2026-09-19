# Changelog

What changed between released versions of `oxn`, and why. Entries describe what a user of the
package would notice; the reasoning behind a change lives in its commit message, and the
reasoning behind a *decision* lives in [`docs/adr/`](docs/adr/).

This file starts at 0.1.0, the first public release. Everything before it happened in a private
repository and is recorded in the commit history and [the phase record](docs/phases.md) rather
than here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `scripts/check.py --release` refused to run when the version being released was already
  tagged, which made it impossible to verify the commit you had just tagged. It now objects
  only when a tag for that version points at code other than `HEAD`.
- Several documentation claims that were false on a public repository: the README stated the
  baseline was empty when it carries four accepted violations, `docs/metrics.md` described
  three shipped language profiles in the future tense, and four ADR links in
  `docs/proposals/` resolved to 404s.

### Added

- [`docs/phases.md`](docs/phases.md), which defines the `P0`–`P12` increment labels used in
  commit subjects and ADR status lines throughout the repository.

### Changed

- Phase labels no longer appear in anything the installed tool prints. `oxn calibration` in
  particular reported the provenance of a threshold using labels a reader had no way to
  resolve.

## [0.1.0] — 2026-09-19

First public release.

### Added

- **The gate.** `oxn check` measures Tier-1 complexity metrics per entity and fails on a
  violation, naming the entity, the rule and the number. `--deep` adds the architectural
  tier: layer contracts and import cycles over the whole graph.
- **Six languages** from one engine — Python, TypeScript, JavaScript, Go, Rust and Java —
  with one shape measuring identically in each.
- **The ratchet.** `oxn baseline` accepts today's debt; recorded debt may not grow, and a new
  violation anywhere fails.
- **Anti-gaming rules.** `shredding` totals a function with the private trivial helpers only
  it calls, so splitting a complex function into one-line helpers does not buy budget. Class
  aggregates (`methods_per_class`, `weighted_methods_per_class`) catch the shapes `shredding`
  cannot see.
- **`oxn init`** wires the `PostToolUse` hook, the MCP server entry and a `CLAUDE.md` section.
  Additive and idempotent — running it twice produces no diff.
- **The MCP server** (`oxn serve`): `get_architectural_context` before writing code,
  `explain_violation` on anything the gate rejects, plus `check_code` and `get_metrics`.
- **`oxn review --base <ref>`** measures what a pull request changed, marking each finding
  `new`, `regression` or `baselined`. It never gates.
- **Reporting surfaces** that never block: `oxn health` (risk profiles), `oxn calibration`
  (every tunable threshold with the evidence behind it), `oxn doctor`, and the metric,
  volume, architecture, calls and class reports.

### Notes

- Runtime dependencies are six, all permissive and pip-installable. The free analysis tools
  OXN could have depended on are used as differential-test oracles in CI instead — see
  [ADR-0001](docs/adr/0001-dependency-policy.md).
- No calibration parameter is fitted to labelled data yet; `oxn calibration` says so per
  parameter rather than presenting judgement as measurement.

[Unreleased]: https://github.com/mustafarslan/oxn/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/mustafarslan/oxn/releases/tag/v0.1.0
