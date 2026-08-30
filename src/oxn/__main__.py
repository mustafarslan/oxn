"""Run OXN as a module: ``python -m oxn``.

Equivalent to the ``oxn`` console script, and useful wherever that script is not on
``PATH`` -- an unactivated virtualenv, a hook whose environment you do not control, ``uv
run``, or a CI step that only knows an interpreter path.

Kept deliberately thin. The PostToolUse hook may invoke OXN this way, so this module must
respect the same rule as :mod:`oxn.cli`: **nothing heavy at import time.**
``tests/test_import_guard.py`` enforces it for both entry points.
"""

from __future__ import annotations

from oxn.cli import main

main()
