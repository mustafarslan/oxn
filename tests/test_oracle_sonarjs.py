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

SONARJS_DIR = os.environ.get("OXN_SONARJS_DIR")
RUNNER = Path(__file__).parent / "oracles" / "sonarjs_runner.mjs"
ESLINT_CONFIG = Path(__file__).parent / "oracles" / "eslint.config.mjs"

requires_sonarjs = pytest.mark.skipif(
    not SONARJS_DIR or not (Path(SONARJS_DIR) / "node_modules").exists(),
    reason="set OXN_SONARJS_DIR to a directory with eslint + eslint-plugin-sonarjs installed",
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
    """Copy the runner and its config beside ``node_modules``.

    ESLint resolves ``eslint-plugin-sonarjs`` relative to the config file, so both must sit
    in the directory where the packages were installed.
    """
    target = Path(SONARJS_DIR or ".")
    runner = target / RUNNER.name
    config = target / ESLINT_CONFIG.name
    shutil.copyfile(RUNNER, runner)
    shutil.copyfile(ESLINT_CONFIG, config)
    return runner, config


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
