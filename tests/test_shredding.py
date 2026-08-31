"""Satisfying a ceiling by splitting instead of simplifying.

This is the gate's own anti-gaming rule, and it exists because the gate was measured
failing without it: a six-branch router scoring cognitive complexity 28 in one function,
split into eight, passed `oxn check` with exit 0 and no findings. Every per-function
maximum fell below the line while the work stayed exactly where it was.

The hard part is not catching the shred. It is catching the shred *without* punishing
decomposition, which is usually the right thing to do. So the tests below are as much about
what must keep passing as about what must fail -- the false positives came first, from a
sweep over `src`, `tests`, `scripts` and a vendored copy of httpx, and each one narrowed the
rule until it fired on the evasion and nothing else.
"""

from __future__ import annotations

import pytest

from oxn.check import EXIT_OK, EXIT_VIOLATIONS, run_check
from oxn.config import Config
from oxn.metrics.shredding import Unit, cluster_totals
from oxn.profiles.python import PYTHON
from oxn.profiles.typescript import TYPESCRIPT

#: The honest article: one function, six branches, genuinely over the ceiling at 28.
HONEST = '''
def route(request):
    """Dispatch a request."""
    method = request.get("method")
    path = request.get("path", "")
    if method == "GET":
        if path.startswith("/users"):
            if path.endswith("/profile"):
                return "user-profile"
            if "?" in path:
                return "user-search"
            return "user-list"
        if path.startswith("/orders"):
            if request.get("admin"):
                return "all-orders"
            return "own-orders"
        return "get-other"
    if method == "POST":
        if path.startswith("/users"):
            if not request.get("body"):
                return "bad-request"
            return "create-user"
        if path.startswith("/orders"):
            if request.get("locked"):
                return "conflict"
            return "create-order"
        return "post-other"
    if method == "DELETE":
        if not request.get("admin"):
            return "forbidden"
        return "delete"
    return "unknown"
'''

#: The same logic, shredded. Every maximum is now 3; nothing was simplified.
SHRED = '''
def _get_users(request, path):
    if path.endswith("/profile"):
        return "user-profile"
    return "user-search" if "?" in path else "user-list"


def _get_orders(request):
    return "all-orders" if request.get("admin") else "own-orders"


def _get(request, path):
    if path.startswith("/users"):
        return _get_users(request, path)
    if path.startswith("/orders"):
        return _get_orders(request)
    return "get-other"


def _post_users(request):
    return "create-user" if request.get("body") else "bad-request"


def _post_orders(request):
    return "conflict" if request.get("locked") else "create-order"


def _post(request, path):
    if path.startswith("/users"):
        return _post_users(request)
    if path.startswith("/orders"):
        return _post_orders(request)
    return "post-other"


def _delete(request):
    return "delete" if request.get("admin") else "forbidden"


def route(request):
    """Dispatch a request."""
    method = request.get("method")
    path = request.get("path", "")
    if method == "GET":
        return _get(request, path)
    if method == "POST":
        return _post(request, path)
    if method == "DELETE":
        return _delete(request)
    return "unknown"
'''

#: The real fix: a table replaces the branching. Fewer helpers, none of them trivial,
#: and the complexity is gone rather than relocated.
COHESIVE = '''
ROUTES = {
    ("GET", "/users"): "user-list",
    ("GET", "/orders"): "own-orders",
    ("POST", "/users"): "create-user",
    ("POST", "/orders"): "create-order",
    ("DELETE", ""): "delete",
}


def _prefix(path):
    for known in ("/users", "/orders"):
        if path.startswith(known):
            return known
    return ""


def route(request):
    """Dispatch a request."""
    method = request.get("method")
    prefix = _prefix(request.get("path", ""))
    return ROUTES.get((method, prefix), "unknown")
'''


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _rules(report):
    return {finding.rule for finding in report.blocking}


# ---- the evasion, and the measurement that found it ------------------------------------


def test_the_honest_function_is_a_violation(project) -> None:
    """The motive. Without a real violation there is nothing to evade."""
    (project / "honest.py").write_text(HONEST)
    report = run_check(["honest.py"], use_baseline=False)
    assert "cognitive_complexity" in _rules(report)


def test_shredding_that_function_no_longer_passes(project) -> None:
    """The regression this whole rule exists for: this used to exit 0, cleanly."""
    (project / "shred.py").write_text(SHRED)
    report = run_check(["shred.py"], use_baseline=False)
    assert report.exit_code == EXIT_VIOLATIONS
    assert "shredding" in _rules(report)


def test_the_finding_names_the_helpers_to_put_back(project) -> None:
    """A number alone tells an agent it failed, not what to do."""
    (project / "shred.py").write_text(SHRED)
    finding = next(
        f for f in run_check(["shred.py"], use_baseline=False).blocking if f.rule == "shredding"
    )
    trail = " ".join(finding.explanation)
    assert "_get_users" in trail and "_delete" in trail
    assert finding.entity.endswith("route"), "the finding belongs to the root, not a helper"


def test_actually_fixing_it_passes(project) -> None:
    """The rule must leave a real fix a way through, or it is a trap rather than a gate."""
    (project / "fixed.py").write_text(COHESIVE)
    report = run_check(["fixed.py"], use_baseline=False)
    assert report.exit_code == EXIT_OK, [str(f) for f in report.blocking]


# ---- what must keep passing ------------------------------------------------------------


def test_a_big_file_does_not_dilute_the_finding(project) -> None:
    """Scoped to the caller and its helpers, never to the file.

    An earlier version of this rule compared trivial helpers against the file's function
    count, which fired on a 10-function demo and missed the same shred pasted into a
    200-function module -- that is, it worked everywhere except real code.
    """
    padding = "".join(
        f"\n\ndef unrelated_{n}(value):\n    return value + {n}\n" for n in range(200)
    )
    (project / "big.py").write_text(SHRED + padding)
    assert "shredding" in _rules(run_check(["big.py"], use_baseline=False))


def test_a_shared_helper_is_not_a_dedicated_one(project) -> None:
    """Two callers make it shared code, which is the thing decomposition is *for*."""
    source = SHRED + "\n\ndef also(request):\n    return _delete(request) or _get_orders(request)\n"
    (project / "shared.py").write_text(source)
    findings = [
        f for f in run_check(["shared.py"], use_baseline=False).blocking if f.rule == "shredding"
    ]
    assert not findings


def test_splitting_something_that_was_never_a_violation_is_fine(project) -> None:
    """The rule gates ceiling evasion, not style. With no ceiling breached, nothing is."""
    source = (
        "".join(f"\ndef _step_{n}(value):\n    return value + {n}\n" for n in range(5))
        + "\n\ndef run(value):\n    return "
        + " + ".join(f"_step_{n}(value)" for n in range(5))
        + "\n"
    )
    (project / "small.py").write_text(source)
    assert run_check(["small.py"], use_baseline=False).exit_code == EXIT_OK


def test_the_rule_follows_a_configured_ceiling_not_just_the_default(project) -> None:
    """An anti-gaming rule has no number of its own.

    It exists to stop one ceiling being satisfied dishonestly, so it must move when a
    project moves that ceiling. Sharing only the *default* would tell a project that raised
    `cognitive_complexity` to 20 that a cluster of 13 is shredding, while a plain 19-point
    function sailed through -- blocking an honest split in the name of stopping a dishonest
    one. OXN's own oxn.yaml uses the default, so dogfooding could never catch this.
    """
    (project / "shred.py").write_text(SHRED)
    assert "shredding" in _rules(run_check(["shred.py"], use_baseline=False))

    (project / "oxn.yaml").write_text("ceilings:\n  cognitive_complexity: 20\n")
    report = run_check(["shred.py"], config=Config.load(project), use_baseline=False)
    assert report.exit_code == EXIT_OK, [str(f) for f in report.blocking]


# ---- the unit rule, where the edges are cheap to state ---------------------------------


def _unit(name, score, calls=()):
    return Unit(entity_id=f"id:{name}", name=name, score=score, calls=tuple(calls))


def test_a_name_defined_twice_declines_rather_than_guesses(project) -> None:
    """Two `_helper` methods on different classes collide in a bare-name index.

    Failing safe here is deliberate and must stay that way: the rule declines to fire
    rather than attribute a call to the wrong definition. Anyone "fixing" this into a
    resolution guess turns a conservative rule into a false-positive source.
    """
    units = [
        _unit("root", 6.0, ["_a", "_b", "_dup"]),
        _unit("_a", 1.0),
        _unit("_b", 1.0),
        _unit("_dup", 1.0),
        _unit("_dup", 1.0),
    ]
    assert cluster_totals(units, PYTHON) == {}


def test_a_language_without_a_privacy_rule_does_not_fire() -> None:
    """TypeScript spells visibility with modifiers, not names.

    "Called once in this file" does not prove a public name has no callers elsewhere, so
    single-use without privacy is unsound. The profile declines instead of guessing.
    """
    units = [
        _unit("root", 8.0, ["_a", "_b", "_c"]),
        _unit("_a", 1.0),
        _unit("_b", 1.0),
        _unit("_c", 1.0),
    ]
    assert cluster_totals(units, PYTHON), "control: python does fire on this shape"
    assert cluster_totals(units, TYPESCRIPT) == {}


def test_helpers_fold_transitively_onto_one_root() -> None:
    """Shredding nests. Counting only direct children scores one act as three small ones."""
    units = [
        _unit("root", 3.0, ["_get", "_post"]),
        _unit("_get", 2.0, ["_get_a", "_get_b"]),
        _unit("_post", 2.0, ["_post_a"]),
        _unit("_get_a", 1.0),
        _unit("_get_b", 1.0),
        _unit("_post_a", 1.0),
    ]
    clusters = cluster_totals(units, PYTHON)
    assert len(clusters) == 1
    assert clusters["id:root"].total == 10.0
    assert len(clusters["id:root"].helpers) == 5
