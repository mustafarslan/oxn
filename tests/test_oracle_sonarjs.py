"""JavaScript cognitive complexity against eslint-plugin-sonarjs.

SonarSource wrote both the specification and this plugin, so it is the closest thing to a
reference implementation that exists -- which is why the divergences are enumerated rather
than tolerated."""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from oxn.languages import get_parser
from oxn.metrics import cognitive_complexity
from oxn.profiles import get_profile

pytestmark = pytest.mark.oracle

ORACLE_DIR = Path(__file__).parent / "oracles"
RUNNER = ORACLE_DIR / "sonarjs_runner.mjs"
ESLINT_CONFIG = ORACLE_DIR / "eslint.config.mjs"

#: Defaults to where `scripts/check.py --oracle --install` puts `node_modules`, rather than
#: requiring an environment variable to be set correctly. Requiring one is how seventeen
#: differential tests sat skipped long enough for the harness itself to rot: a lane that
#: only runs when a variable happens to be exported does not run.
SONARJS_DIR = os.environ.get("OXN_SONARJS_DIR") or str(ORACLE_DIR)

requires_sonarjs = pytest.mark.skipif(
    not (Path(SONARJS_DIR) / "node_modules").exists(),
    reason="run `python scripts/check.py --oracle --install` to fetch eslint + sonarjs",
)

#: Cases where OXN follows the published specification and sonarjs 4.2.0 does not.
#: Each is adjudicated in docs/divergences.md; the third is agreement, kept as a guard.
SONARJS_DIVERGENCES = {
    # The white paper prints this exact expression as 4: +1 for the `if`, +1 per operator run.
    "function f(a,b,c,d,e,g){ if(a&&b&&c||d||e&&g){} }": (4, 3),
    # Appendix B lists "each method in a recursion cycle"; sonarjs does not implement it.
    "function f(a){ return f(a-1); }": (1, None),
}


@functools.lru_cache(maxsize=1)
def _staged_runner() -> tuple[Path, Path]:
    """Put the runner and its config beside ``node_modules``.

    ESLint resolves ``eslint-plugin-sonarjs`` relative to the config file, so both must sit
    in the directory where the packages were installed. Usually they already do:
    `scripts/check.py` installs into `tests/oracles`, which is where the runner lives and
    what it sets `OXN_SONARJS_DIR` to. Copying a file onto itself raises `SameFileError`
    rather than being a no-op, so this stages *if needed*.
    """
    target = Path(SONARJS_DIR or ".")
    return _beside(RUNNER, target), _beside(ESLINT_CONFIG, target)


def _beside(source: Path, target: Path) -> Path:
    destination = target / source.name
    if not (destination.exists() and destination.samefile(source)):
        shutil.copyfile(source, destination)
    return destination


def _sonarjs_scores(source: str) -> list[int]:
    runner, config = _staged_runner()
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, dir=SONARJS_DIR) as handle:
        handle.write(source)
        path = handle.name
    try:
        result = subprocess.run(
            ["node", str(runner), path, str(config)],
            capture_output=True,
            text=True,
            cwd=SONARJS_DIR,
            check=True,
        )
        return sorted(item["score"] for item in json.loads(result.stdout.strip() or "[]"))
    finally:
        os.unlink(path)


def _oxn_js_scores(source: str) -> list[int]:
    profile = get_profile("javascript")
    root = get_parser("javascript").parse(source.encode()).root_node
    scores: list[int] = []

    def walk(node) -> None:
        for child in node.named_children:
            definition = profile.unwrap(child)
            if definition.type in profile.function_like:
                scores.append(
                    cognitive_complexity(
                        definition, profile, function_name=profile.entity_name(definition)
                    ).score
                )
            walk(child)

    walk(root)
    return sorted(score for score in scores if score > 0)


@requires_sonarjs
@pytest.mark.parametrize(
    "source",
    [
        "function f(a){ if(a){} }",
        "function f(a){ if(a){} else {} }",
        "function f(a){ if(a){} else if(a){} else {} }",
        "function f(a){ if(a){} else if(a){ if(a){} } }",
        "function f(a){ if(a){} else if(a){} else if(a){ for(const x of a){ if(x){} } } }",
        "function f(a,b){ if(a>0){ for(let i=0;i<a;i++){ if(b>i){ g(); } } } }",
        "function f(a){ switch(a){ case 1: break; case 2: break; default: break; } }",
        "function f(){ try{} catch(e){} finally{} }",
        "function f(a){ try{ if(a){} } catch(e){ if(a){} } }",
        "function f(a,b,c){ if(a && !(b && c)){} }",
        "function f(a){ if(a > 0){} }",
        "function f(a){ return a ? 1 : 2; }",
        "function f(a){ while(a){} do {} while(a); }",
        "function f(a){ outer: for(const x of a){ for(const y of a){ break outer; } } }",
        "function f(a){ if(a){ if(a){ if(a){ if(a){} } } } }",
    ],
)
def test_javascript_matches_sonarjs(source: str) -> None:
    assert _oxn_js_scores(source) == _sonarjs_scores(source)


@requires_sonarjs
@pytest.mark.parametrize("source", sorted(SONARJS_DIVERGENCES))
def test_documented_sonarjs_divergences_still_hold(source: str) -> None:
    """OXN follows the specification here and sonarjs does not. See docs/divergences.md."""
    expected_oxn, expected_sonarjs = SONARJS_DIVERGENCES[source]
    scores = _sonarjs_scores(source)
    assert _oxn_js_scores(source) == [expected_oxn]
    if expected_sonarjs is None:
        assert scores == [], "sonarjs implemented this; revisit docs/divergences.md"
    else:
        assert scores == [expected_sonarjs], "sonarjs changed; revisit docs/divergences.md"
