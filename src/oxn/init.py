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
* `.mcp.json` is merged the same way, and an `oxn` entry someone has edited is left alone.

`.mcp.json` was withheld through P2.5 on the grounds that wiring a client to a server that
does not exist produces a broken tool in someone's editor and a bug report about OXN. The
server exists as of P9, so it is written -- and the rule it was withheld under is the reason
it may be written now.

**Both wirings name `oxn`, not a path**, and that is a decision rather than an oversight.
`.mcp.json` is committed and shared: an absolute interpreter path resolves on the machine
that ran `init` and on no one else's. The cost is that `oxn` must be on the `PATH` of the
shell a hook or an MCP client is started with, which is *not* true for a project installed
only into a virtualenv -- and a hook that cannot start is a gate that silently is not there.
`init` cannot fix that without pinning a path it must not pin, so it detects the case and
says so in its report. Silently absent was the failure; visibly absent is not.
"""

from __future__ import annotations

import json
import os
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

#: The MCP server entry, in the shape every MCP client reads. `oxn serve` rather than an
#: interpreter path: see the module docstring.
MCP_SERVER: dict[str, object] = {"command": "oxn", "args": ["serve"]}

CLAUDE_SECTION = f"""{MARKER_BEGIN}
## Architecture and quality invariants (OXN)

This repository is gated by [OXN](https://github.com/mustafarslan/oxn). A `PostToolUse`
hook runs `{HOOK_COMMAND}` after every edit; a non-zero exit means the edit broke an
invariant, and the JSON names the entity, the rule and the number.

* Ceilings and layer rules live in `{CONFIG_NAME}`. Read it before assuming a limit. Layer
  contracts are checked on the edit that breaks them, not only in CI, so an import that
  reaches across a boundary is rejected with the offending edge named.
* A violation is not a suggestion. Fix the cause -- do not split a function into one-line
  helpers to get under a ceiling. That is reported as rule `shredding`, which totals a
  function together with the private, trivial helpers only it calls: dedicated helpers do
  not raise the budget. "Private" is read from the language -- an underscore in Python,
  casing in Go, `pub` in Rust, `private` in Java and TypeScript, `#` or a module that does
  not export it in JavaScript. A CommonJS file is the one case the rule declines: anything
  can be published there by assigning to `module.exports`, so a helper cannot be shown to be
  dedicated, and the class aggregates below are what catch a shred in that shape.
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
{MARKER_END}"""

STARTER_CONFIG = f"""# OXN — architecture and quality invariants for this repository.
# Everything here is optional: with no {CONFIG_NAME} at all, OXN enforces the documented
# defaults in `oxn.thresholds`. Uncomment what you want to change.

# Every rule OXN gates on, with its default. A number changes the limit; `off` removes the
# rule. Listed in full rather than by exception, so that what this project decided is
# readable in one place -- a rule you never think about is a rule you never chose.
# ceilings:
#   cognitive_complexity: 12          # per function; the headline gate
#   cyclomatic_complexity: 10         # 98 of its 351 catches are its own (six corpora)
#   max_nesting_depth: 4              # caught 25 across six corpora, 0 that cognitive missed
#   parameter_count: 5
#   function_sloc: 60
#   file_sloc: 500
#   methods_per_class: 12             # the class ratchet; see `.oxn/baseline.json`
#   weighted_methods_per_class: 25
#   shredding: 12                     # follows cognitive_complexity; `off` there turns
#                                     # this off too, since it exists to protect that gate

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

# Code that is not yours to measure. OXN already skips the directories nobody writes by
# hand -- node_modules, dist, build, vendor, third_party, target, .venv and friends -- but
# generated *files* live beside hand-written ones and have to be named. Nothing here is
# looked at at all, so a ceiling can never fire on it.
# exclude:
#   - "**/*_pb2.py"          # protobuf
#   - "**/*_pb2_grpc.py"
#   - "**/*.min.js"          # bundled or minified output
#   - "**/*.generated.ts"    # codegen: graphql, openapi, prisma
#   - "src/migrations/*"

# Paths that are reported but never block. Distinct from `exclude`: a finding here is
# measured and shown, it just cannot fail the build.
# advisory:
#   - "tests/fixtures/*"

# How many repairs an agent may spend on one violation before the hook stops asking for a
# fix and tells it to escalate instead. LLM refactoring does not always converge, and an
# unbounded gate is an infinite loop with a token budget attached. 0 disables the bound,
# which is what CI wants: there is no agent there to escalate to.
# retry_budget: 3
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


def run_init(
    root: Path | None = None, *, with_hook: bool = True, with_mcp: bool = True
) -> InitReport:
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
    if with_mcp:
        _write_mcp(base, report)
    else:
        report.notes.append(".mcp.json not written (--no-mcp)")
    _write_gitignore(base, report)
    _note_how_oxn_resolves(base, report)
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


def _write_mcp(base: Path, report: InitReport) -> None:
    """Merge OXN's server into `.mcp.json`, keeping every other server and any hand edit."""
    path = base / ".mcp.json"
    config: dict[str, object] = {}
    if path.exists():
        loaded = _json_object(path, report)
        if loaded is None:
            return
        config = loaded

    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        report.notes.append("`mcpServers` in .mcp.json is not an object; left alone")
        return
    if "oxn" in servers:
        # Equal means a previous run wrote it; different means someone changed it on
        # purpose -- a wrapper script, a pinned interpreter -- and that is theirs to keep.
        if servers["oxn"] != MCP_SERVER:
            report.notes.append(".mcp.json already configures `oxn` differently; left alone")
        else:
            report.unchanged.append(".mcp.json")
        return

    servers["oxn"] = dict(MCP_SERVER)
    existed = path.exists()
    path.write_text(json.dumps(config, indent=2) + "\n")
    (report.updated if existed else report.created).append(".mcp.json")


def _json_object(path: Path, report: InitReport | None = None) -> dict[str, object] | None:
    """Read a JSON object, or say why it was left alone. Never raises at a user.

    With no `report` this is a quiet read for a file that may not be there: the caller is
    inspecting what is already wired, not proposing to write it.
    """
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text())
    except json.JSONDecodeError:
        if report:
            report.notes.append(f"{path.name} is not valid JSON; it was left alone")
        return None
    if not isinstance(loaded, dict):
        if report:
            report.notes.append(f"{path.name} is not an object; it was left alone")
        return None
    return loaded


def _wirings_that_look_oxn_up_on_the_path(base: Path) -> bool:
    """Does anything `init` wired still depend on `oxn` being on PATH?

    A note saying "an MCP client will not find it" is simply false once the entry names a
    path, and a tool that says it anyway is a tool people stop reading. Both wirings are
    matched on the *bare* command: JSON-encoded, `"oxn check --json"` cannot match a
    `"…/bin/oxn check --json"` that someone has already fixed.
    """
    settings = json.dumps(_json_object(base / ".claude" / "settings.json") or {})
    servers = (_json_object(base / ".mcp.json") or {}).get("mcpServers")
    entry = servers.get("oxn") if isinstance(servers, dict) else None
    command = entry.get("command") if isinstance(entry, dict) else None
    return f'"{HOOK_COMMAND}"' in settings or command == MCP_SERVER["command"]


def _note_how_oxn_resolves(base: Path, report: InitReport) -> None:
    """Say it when `oxn` will not be on the PATH of the shells that have to run it.

    The hook and the MCP client are started by an editor, not by the terminal that ran
    `oxn init`, and an editor launched from the desktop inherits none of a virtualenv's
    activation.

    The test is *activation*, not `sys.prefix != sys.base_prefix`. A `pipx install` and a
    `uv tool install` both put OXN in a virtualenv too -- and they are the fix, so a prefix
    check greets the people who already did the right thing by telling them to do it. What
    distinguishes the broken case is that `oxn` is on this shell's PATH only because the
    shell was activated (`VIRTUAL_ENV`), or is not on it at all -- and that only matters
    while something OXN wired is still looking `oxn` up on PATH.
    """
    import shutil

    if not _wirings_that_look_oxn_up_on_the_path(base):
        return
    activated = os.environ.get("VIRTUAL_ENV")
    if not activated and shutil.which("oxn") is not None:
        return
    where = f" at {activated}" if activated else ""
    report.notes.append(
        f"`oxn` is not on this shell's PATH independently of a virtualenv{where}, so a "
        "PostToolUse hook or an MCP client started by an editor will not find it. Install "
        "it with pipx or `pip install --user`, or point the hook command and the `oxn` "
        "entry in .mcp.json at a path that resolves. Claude Code expands variables in "
        '.mcp.json, so `"command": "${CLAUDE_PROJECT_DIR:-.}/.venv/bin/oxn"` names the venv '
        "without naming your machine -- and a hand-edited entry is left alone on re-run."
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
