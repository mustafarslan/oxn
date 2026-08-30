"""The hook fast path must stay cheap.

OXN runs inside a Claude Code PostToolUse hook, once per agent edit, as a *cold process*
(ADR-0002). The budget is p95 <= 200 ms per file. Measured on the development machine:
a bare interpreter costs ~14 ms, ``import typer`` adds ~23 ms, ``import rich.console``
~15 ms and ``import networkx`` ~146 ms.

These tests fail the build the moment someone puts a heavy import at module level. They
are the reason the roadmap can promise a latency budget at all.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from oxn.cli import FORBIDDEN_ON_FAST_PATH

#: Never acceptable anywhere in OXN's runtime, at any depth. ADR-0001 self-implements the
#: graph algorithms specifically so that networkx never appears here.
NEVER_IMPORTABLE = frozenset({"networkx", "numpy", "torch", "pandas", "scipy"})


def _modules_after(code: str) -> set[str]:
    """Run ``code`` in a fresh interpreter and return the top-level modules it loaded."""
    dump = "import json,sys;print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))"
    probe = f"{code}\n{dump}"
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    return set(json.loads(out.stdout.strip().splitlines()[-1]))


def test_importing_oxn_is_free() -> None:
    """``import oxn`` must pull in nothing but the standard library."""
    loaded = _modules_after("import oxn")
    assert not (loaded & FORBIDDEN_ON_FAST_PATH), (
        f"`import oxn` pulled in heavy modules: {sorted(loaded & FORBIDDEN_ON_FAST_PATH)}"
    )


def test_importing_cli_is_free() -> None:
    """``import oxn.cli`` is what the entry point does before dispatching. Keep it clean."""
    loaded = _modules_after("import oxn.cli")
    assert not (loaded & FORBIDDEN_ON_FAST_PATH), (
        f"`import oxn.cli` pulled in heavy modules: {sorted(loaded & FORBIDDEN_ON_FAST_PATH)}. "
        "Move the import inside the function that needs it."
    )


def test_module_entry_point_stays_on_the_fast_path() -> None:
    """``python -m oxn check --json`` is a hook-callable entry point too."""
    loaded = _modules_after(
        "import runpy, sys\n"
        "sys.argv = ['oxn', 'check', '--json']\n"
        "try:\n"
        "    runpy.run_module('oxn', run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass"
    )
    offenders = loaded & FORBIDDEN_ON_FAST_PATH
    assert not offenders, f"`python -m oxn check --json` loaded {sorted(offenders)}"


def test_fast_path_never_imports_typer_or_rich() -> None:
    """The real thing: running ``oxn check --json`` must not touch the human-path stack."""
    loaded = _modules_after(
        "import oxn.cli\ntry:\n    oxn.cli.main(['check', '--json'])\nexcept SystemExit:\n    pass"
    )
    offenders = loaded & FORBIDDEN_ON_FAST_PATH
    assert not offenders, (
        f"`oxn check --json` loaded {sorted(offenders)}. Every module here costs the hook "
        "latency budget on every single agent edit."
    )


@pytest.mark.parametrize("module", sorted(NEVER_IMPORTABLE))
def test_forbidden_modules_are_not_runtime_dependencies(module: str) -> None:
    """These must never become runtime dependencies, so importing them may simply fail."""
    loaded = _modules_after("import oxn._app" if module == "typer" else "import oxn.cli")
    assert module not in loaded
