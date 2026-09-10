"""Terminal output for the harness, defined once.

`dogfood.py` and `summary.py` each had their own `say` and their own copy of the palette,
which is two definitions of one thing and the reason `summary`'s had no yellow. Extracting
them also breaks the import cycle a third module would otherwise create: `attempt.py` needs
to print, and importing `say` from `dogfood` while `dogfood` imports the attempt loop is not
an arrangement Python allows.
"""

from __future__ import annotations

DIM, GREEN, RED, YELLOW, BOLD, RESET = (
    "\033[2m",
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[1m",
    "\033[0m",
)


def say(message: str = "") -> None:
    """Print, flushing immediately.

    A repair run spends minutes inside a single model call, and output buffered through a
    pipe shows nothing until the process exits -- which is indistinguishable from a hang.
    """
    print(message, flush=True)
