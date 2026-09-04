"""`oxn init` — wire OXN into a repository without taking anything away.

Every write here is **additive and idempotent**. That is not politeness; it is the
difference between a tool a person tries once and a tool they leave installed. `idea.md`'s
blueprint proposed overwriting `CLAUDE.md` wholesale, which would destroy hand-written
project instructions on first run — the single most valuable file in an agent-assisted
repository. So:

* `oxn.yaml` is written only when absent, and its content is a commented starting point
  rather than a dump of every default.
* the `CLAUDE.md` section lives between markers and is replaced in place on re-run; text
  outside the markers is never touched.
* `.claude/settings.json` is merged, preserving hooks already configured, and OXN's own
  entry is matched by command so a second run updates rather than duplicates it.

Deliberately **not** written: `.mcp.json`. The original P2.5 plan lists it, but the MCP
server lands in P9 and does not exist yet. Wiring a client to a server that is not there
produces a broken tool in someone's editor and a bug report about OXN, so `init` only wires
surfaces that work today.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from oxn.config import CONFIG_NAME

#: The section `oxn init` owns in `CLAUDE.md`. Everything between these lines is ours to
#: rewrite; everything outside them belongs to whoever wrote it.
MARKER_BEGIN = "<!-- oxn:begin -->"
MARKER_END = "<!-- oxn:end -->"

#: The hook command. Matched literally when deciding whether OXN is already wired, so it
#: must stay stable: changing it silently installs a second hook beside the first.
#:
#: It names no path on purpose. Claude Code sends the `PostToolUse` payload on stdin and
#: `oxn.cli._hook_targets` takes the edited file from it, which keeps this string stable
#: across every existing install while checking one file instead of the tree.
HOOK_COMMAND = "oxn check --json"

CLAUDE_SECTION = f"""{MARKER_BEGIN}
## Architecture and quality invariants (OXN)

This repository is gated by [OXN](https://github.com/mustafarslan/oxn). A `PostToolUse`
hook runs `{HOOK_COMMAND}` after every edit; a non-zero exit means the edit broke an
invariant, and the JSON names the entity, the rule and the number.

* Ceilings and layer rules live in `{CONFIG_NAME}`. Read it before assuming a limit.
* A violation is not a suggestion. Fix the cause -- do not split a function into one-line
  helpers to get under a ceiling. That is reported as rule `shredding`, which totals a
  function together with the private, trivial helpers only it calls: dedicated helpers do
  not raise the budget.
* `.oxn/baseline.json` records pre-existing debt. It may not grow: a baselined violation
  that gets worse fails the build exactly as a new one does.

Run `oxn check` yourself at any time; `oxn check --deep` adds the architectural tier.
{MARKER_END}"""

STARTER_CONFIG = f"""# OXN — architecture and quality invariants for this repository.
# Everything here is optional: with no {CONFIG_NAME} at all, OXN enforces the documented
# defaults in `oxn.thresholds`. Uncomment what you want to change.

# ceilings:
#   cognitive_complexity: 12   # per function; the headline gate
#   cyclomatic_complexity: 10
#   max_nesting_depth: 4
#   parameter_count: 5
#   function_sloc: 60
#   file_sloc: 500

# Name your layers, then constrain how they may depend on each other. Patterns are globs
# matched against repository-relative paths, most specific first.
# layers:
#   domain: ["src/domain/*"]
#   application: ["src/application/*"]
#   infrastructure: ["src/infrastructure/*"]

# contracts:
#   - name: clean-architecture
#     kind: layered
#     # Outermost first: infrastructure may reach application and domain; domain may reach
#     # neither. The direction is easy to read backwards, so it is spelled out.
#     order: [infrastructure, application, domain]

# Generated or vendored code usually wants a different bar from hand-written code.
# layer_ceilings:
#   infrastructure:
#     cognitive_complexity: 20

# Paths that are reported but never block.
# advisory:
#   - "tests/fixtures/*"
"""


@dataclass
class InitReport:
    """What `init` did, so it can say so rather than working silently."""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "notes": self.notes,
        }


def run_init(root: Path | None = None, *, with_hook: bool = True) -> InitReport:
    """Wire OXN into `root`. Safe to run repeatedly, and safe to run on a populated repo.

    With no `root`, this wires the *repository*, not the working directory. `Config.load`
    searches upward for `oxn.yaml`, so an `init` run from `src/` would leave a config that
    governs everything below `src/` and nothing above it -- a second, shadowing project
    inside the first, which is a confusing thing to have created by running a setup command
    in the wrong terminal tab. `.claude/settings.json` and `.gitignore` belong at the
    repository root for the same reason.
    """
    base = Path(root) if root is not None else _repository_root(Path.cwd())
    report = InitReport()

    _write_config(base, report)
    _write_claude_section(base, report)
    if with_hook:
        _write_hook(base, report)
    else:
        report.notes.append("hook not written (--no-hook)")
    _write_gitignore(base, report)
    return report


def _repository_root(start: Path) -> Path:
    """The checkout `start` is inside, or `start` itself when it is not inside one."""
    current = start.resolve()
    for directory in (current, *current.parents):
        if (directory / ".git").exists():
            return directory
    return current


def _write_config(base: Path, report: InitReport) -> None:
    path = base / CONFIG_NAME
    if path.exists():
        report.unchanged.append(CONFIG_NAME)
        return
    path.write_text(STARTER_CONFIG)
    report.created.append(CONFIG_NAME)


def _write_claude_section(base: Path, report: InitReport) -> None:
    """Add or refresh OXN's section, leaving every other line of the file alone."""
    path = base / "CLAUDE.md"
    if not path.exists():
        path.write_text(CLAUDE_SECTION + "\n")
        report.created.append("CLAUDE.md")
        return

    existing = path.read_text()
    if MARKER_BEGIN in existing and MARKER_END in existing:
        head, _, rest = existing.partition(MARKER_BEGIN)
        _, _, tail = rest.partition(MARKER_END)
        updated = head + CLAUDE_SECTION + tail
        if updated == existing:
            report.unchanged.append("CLAUDE.md")
            return
        path.write_text(updated)
        report.updated.append("CLAUDE.md")
        return

    separator = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    path.write_text(existing + separator + CLAUDE_SECTION + "\n")
    report.updated.append("CLAUDE.md")


def _write_hook(base: Path, report: InitReport) -> None:
    """Merge OXN's `PostToolUse` hook into `.claude/settings.json`, keeping any others."""
    path = base / ".claude" / "settings.json"
    settings: dict[str, object] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
        except json.JSONDecodeError:
            report.notes.append(
                ".claude/settings.json is not valid JSON; the hook was not installed"
            )
            return
        if not isinstance(loaded, dict):
            report.notes.append(
                ".claude/settings.json is not an object; the hook was not installed"
            )
            return
        settings = loaded

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        report.notes.append("`hooks` in .claude/settings.json is not an object; left alone")
        return
    entries = hooks.setdefault("PostToolUse", [])
    if not isinstance(entries, list):
        report.notes.append("`hooks.PostToolUse` is not a list; left alone")
        return

    if any(_mentions_oxn(entry) for entry in entries):
        report.unchanged.append(".claude/settings.json")
        return

    entries.append(
        {
            "matcher": "Edit|Write|MultiEdit",
            "hooks": [{"type": "command", "command": HOOK_COMMAND}],
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    path.write_text(json.dumps(settings, indent=2) + "\n")
    (report.updated if existed else report.created).append(".claude/settings.json")


def _mentions_oxn(entry: object) -> bool:
    """Is OXN already wired here? Matched by command, so a re-run updates nothing."""
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks", [])
    if not isinstance(hooks, list):
        return False
    return any(
        isinstance(hook, dict) and HOOK_COMMAND in str(hook.get("command", "")) for hook in hooks
    )


def _write_gitignore(base: Path, report: InitReport) -> None:
    """Ignore the cache, but never the baseline: the ratchet is shared state, not a build
    artifact, and a baseline that is not committed forgives a different set of violations
    for every developer."""
    path = base / ".gitignore"
    wanted = [".oxn/cache/", ".oxn/index/", "!.oxn/baseline.json"]
    existing = path.read_text() if path.exists() else ""
    missing = [line for line in wanted if line not in existing.splitlines()]
    if not missing:
        report.unchanged.append(".gitignore")
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    block = "\n".join(["", "# OXN: the cache is derived, the baseline is shared state.", *missing])
    path.write_text(existing + prefix + block + "\n")
    (report.updated if existing else report.created).append(".gitignore")
