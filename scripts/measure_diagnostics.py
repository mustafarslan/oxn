"""How large the message is that OXN hands an agent when it rejects an edit (P12).

    python scripts/measure_diagnostics.py benchmarks/corpora/python-httpx

**This is the measurement that declined the SLM diagnostic compressor.** P12 proposed a local
Qwen-2.5-Coder pass to compress the hook's diagnostic before the agent reads it. Before
choosing a model, the thing it would compress had to be measured -- and it is small. The
compressor would also have to run where the diagnostic is produced, and ADR-0002 budgets that
path at p95 <= 200 ms for a cold process, which no model call fits inside.

The diagnostic is what `oxn check --json` writes to stderr: the sentence the agent acts on.
The JSON on stdout is the machine half and is not what a compressor would target.

**Measured 2026-09-14**, every source file of both corpora:

=================  =====  ========  =====  ======  =====  =====
corpus             files  rejected  min    median  p95    max
=================  =====  ========  =====  ======  =====  =====
`python-httpx`        60        22    388     737  2,263  2,546
`typescript-nest`  1,913       309    421     594  1,686  4,461
=================  =====  ========  =====  ======  =====  =====

Bytes. Roughly 100 to 1,100 tokens on the edit that was rejected, and the median case in both
languages is under 800 bytes -- about 150 tokens. There is no payload problem here to solve.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent
#: The installed entry point, not `python -m`, because that is what a hook invokes.
OXN = ROOT / ".venv" / "bin" / "oxn"
#: Every file, not a sample. A first draft measured "the first 250" of `typescript-nest` and
#: got a different answer depending on whether the walk was sorted -- 23 rejected edits or 37,
#: with medians of 455 and 691 bytes. A distribution that depends on directory order is not a
#: measurement, and the whole corpus costs a few minutes once.


def _diagnostic_sizes(project: Path, suffix: str) -> list[int]:
    """The stderr byte count of every rejected edit, one cold `oxn check --json` per file."""
    sizes = []
    for path in sorted(project.rglob(f"*{suffix}")):
        done = subprocess.run(  # noqa: S603 - fixed argv, paths from the caller's own tree
            [str(OXN), "check", "--json", str(path.relative_to(project))],
            cwd=project,
            capture_output=True,
        )
        if done.stderr:
            sizes.append(len(done.stderr))
    return sorted(sizes)


def _report(project: Path, suffix: str) -> None:
    sizes = _diagnostic_sizes(project, suffix)
    if not sizes:
        print(f"{project.name}: no edit was rejected")
        return
    p95 = sizes[min(len(sizes) - 1, int(0.95 * len(sizes)))]
    print(
        f"{project.name:24} rejected={len(sizes):4}  min={sizes[0]:,}  "
        f"median={int(median(sizes)):,}  p95={p95:,}  max={sizes[-1]:,} bytes"
    )


def main(argv: list[str]) -> int:
    #: Suffix per corpus, since the point is that the answer is the same in both languages.
    suffixes = {"typescript-nest": ".ts"}
    for raw in argv or [str(ROOT / "benchmarks" / "corpora" / "python-httpx")]:
        project = Path(raw).resolve()
        _report(project, suffixes.get(project.name, ".py"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
