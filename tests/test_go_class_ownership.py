"""Go declares methods beside their type, not inside it, and the aggregates must see that.

Every other language OXN supports nests a method in its type's body, so containment answers
"which class owns this". Go writes `func (c *Counter) Inc()` at file scope, and until this
landed the consequences were three deep:

* a `type_declaration` carries no name -- the name is on the `type_spec` it wraps -- so every
  Go type was an unnamed entity, and a grouped `type ( Alpha ...; Beta ... )` collapsed to one;
* a `method_declaration` was classified `function`, because the method kinds were TypeScript
  and Java node names and Go's type has no body to be "inside";
* so `nom` and `wmc` were 0 for all 379 of go-kit's structs, and both class ceilings were
  **silently inert for the language** -- a gate reporting a clean pass having checked nothing.

The join is by *package*, and that is a language guarantee rather than a convenience: Go
requires a method to be declared in the same package as its receiver type. It matters even
though go-kit never exercises it -- all 376 of its methods sit in the same file as their type
-- because a file-scoped answer would change value when a package was split across files, and
a ceiling is compared against `.oxn/baseline.json`. An aggregate that means different things
on different days cannot be compared against anything.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

COUNTER = """package metrics

type Counter struct {
\tname string
\tn    int
}

func (c *Counter) Inc() { c.n++ }

func (c *Counter) Add(delta int) {
\tif delta > 0 {
\t\tc.n += delta
\t}
}

func (c Counter) Name() string { return c.name }

type Gauge struct{ v float64 }

func (g *Gauge) Set(v float64) { g.v = v }

func NewCounter(name string) *Counter { return &Counter{name: name} }
"""

#: The same type's methods, in a sibling file of the same package.
COUNTER_JSON = """package metrics

func (c *Counter) MarshalJSON() ([]byte, error) {
\tif c.n < 0 {
\t\treturn nil, nil
\t}
\treturn []byte("{}"), nil
}

func (c *Counter) Reset() { c.n = 0 }
"""

GROUPED = """package metrics

type (
\tAlpha struct{ x int }
\tBeta  struct{ y int }
)
"""


def _entities(source: str, name: str = "m.go"):
    from oxn.graph.builder import build_file
    from oxn.languages import get_parser
    from oxn.profiles import get_profile

    profile = get_profile("go")
    data = source.encode()
    tree = get_parser("go").parse(data)
    return list(build_file(name, data, profile, tree.root_node).entities)


def _totals(package: Path) -> dict[str, tuple[float, float]]:
    """(nom, wmc) by qualified name, through the real indexing path."""
    from oxn.report import run_metrics

    run_metrics([str(package)], limit=1)
    conn = sqlite3.connect(package / ".oxn/cache/graph.db")
    rows = conn.execute(
        "SELECT e.qualified_name,"
        " MAX(CASE WHEN m.metric_key = 'nom' THEN m.value END),"
        " MAX(CASE WHEN m.metric_key = 'wmc' THEN m.value END)"
        " FROM entities e JOIN metrics m ON m.entity_id = e.id"
        " WHERE e.kind IN ('class', 'interface') GROUP BY e.id"
    ).fetchall()
    return {name: (nom, wmc) for name, nom, wmc in rows}


@pytest.fixture
def package(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_a_go_type_is_named_by_its_spec_not_its_declaration() -> None:
    """`type_declaration` has no name field; the `type_spec` it wraps has it."""
    classes = [e for e in _entities(COUNTER) if e.kind.value == "class"]
    assert [e.name for e in classes] == ["Counter", "Gauge"]


def test_a_grouped_type_declaration_is_two_types() -> None:
    """`type ( Alpha ...; Beta ... )` is one declaration and two types.

    Recording the outer node produced a single unnamed class per block. Unwrapping to the
    first spec would have been worse -- silently correct for the common case and silently
    lossy here -- which is why the outer kind was dropped instead.
    """
    classes = [e for e in _entities(GROUPED) if e.kind.value == "class"]
    assert sorted(e.name or "" for e in classes) == ["Alpha", "Beta"]


def test_a_receiver_makes_a_declaration_a_method() -> None:
    """And a plain function stays a function, which is the half that can go wrong quietly."""
    by_name = {e.name: e for e in _entities(COUNTER) if e.name}
    assert [by_name[n].kind.value for n in ("Inc", "Add", "Name", "Set")] == ["method"] * 4
    assert by_name["NewCounter"].kind.value == "function"
    assert by_name["Inc"].attrs["receiver_type"] == "Counter"


def test_pointer_and_value_receivers_name_the_same_type() -> None:
    """`(c *Counter)` and `(c Counter)` are both methods of `Counter`, not two types."""
    receivers = {e.name: e.attrs.get("receiver_type") for e in _entities(COUNTER) if e.name}
    assert receivers["Inc"] == receivers["Name"] == "Counter"


def test_methods_in_a_sibling_file_still_belong_to_their_type(package: Path) -> None:
    """The case the package-scope join exists for, and the one go-kit never exercises.

    `Counter` is declared in one file with three methods and gains two more in another. A
    file-scoped aggregate reports 3, and would report 5 the day someone merged the files --
    the same class, the same code, a different number.
    """
    (package / "counter.go").write_text(COUNTER)
    (package / "counter_json.go").write_text(COUNTER_JSON)

    totals = _totals(package)
    assert totals["counter.Counter"][0] == 5, "three methods here, two in the sibling file"
    assert totals["counter.Gauge"][0] == 1


def test_the_aggregate_does_not_move_when_the_package_is_split(package: Path) -> None:
    """The ratchet's premise: the same code measures the same, however it is filed."""
    (package / "all.go").write_text(COUNTER + COUNTER_JSON.split("\n", 2)[2])
    together = _totals(package)["all.Counter"]

    for stale in package.glob("*.go"):
        stale.unlink()
    (package / "counter.go").write_text(COUNTER)
    (package / "counter_json.go").write_text(COUNTER_JSON)
    apart = _totals(package)["counter.Counter"]

    assert together == apart, f"{together} together but {apart} split across files"
