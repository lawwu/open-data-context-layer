"""SQL definitions from a JSON Lines file, read the way a Dataform compilation result is."""
import json

import pytest

pytest.importorskip("sqlglot")
from odcl import Catalog, CatalogError  # noqa: E402
from odcl.cli import main  # noqa: E402
from odcl.sqldefs import import_definitions  # noqa: E402

LINES = [
    {"target": "hub.raw.shifts", "declaration": True, "description": "Shifts, as the scheduler exports them"},
    {"target": "hub.summary.by_org", "type": "view", "file": "views/by_org.sql", "description": "Hours and shifts by organisation",
     "select": "WITH s AS (SELECT * FROM Hub.RAW.Shifts) SELECT org_id, SUM(minutes) / 60 AS hours, COUNT(*) AS shifts FROM s GROUP BY org_id",
     "columns": {"hours": "Worked hours"}},
]


@pytest.fixture
def catalog(database):
    with Catalog(database, actor="ana") as c:
        c.put("column", {"table": c.put("table", {"path": "hub.raw.shifts"})["key"], "name": "org_id"})
        c.put("column", {"table": "hub.raw.shifts", "name": "minutes", "dtype": "INT64"})
        yield c


def write(tmp_path, lines):
    path = tmp_path / "definitions.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n\n")
    return str(path)


def test_each_column_is_traced_through_the_select_to_the_columns_it_is_computed_from(catalog, tmp_path):
    result = import_definitions(catalog, write(tmp_path, LINES))
    assert result["definitions"] == 2 and (result["columns_traced"], result["columns_partial"]) == (2, 1)
    view = catalog.get("hub.summary.by_org")
    assert view["description"] == "Hours and shifts by organisation" and view["type"] == "view"
    assert [t["key"] for t in view["derived_from"]] == ["table:hub.raw.shifts"]
    assert view["source"]["sql_definitions"]["file"] == "views/by_org.sql"
    hours = catalog.get("hub.summary.by_org.hours")
    assert hours["description"] == "Worked hours" and "SUM(minutes)" in hours["definition"] and hours["tags"] == []
    assert [d["key"] for d in hours["derived_from"]] == ["column:hub.raw.shifts.minutes"]  # through the CTE's SELECT *, whatever the case
    shifts = catalog.get("hub.summary.by_org.shifts")
    assert shifts["tags"] == ["lineage:partial"] and [d["key"] for d in shifts["derived_from"]] == ["column:hub.raw.shifts.org_id"]
    raw = catalog.get("hub.raw.shifts")
    assert raw["description"] == "Shifts, as the scheduler exports them" and catalog.get("hub.raw.shifts.minutes")["dtype"] == "INT64"  # merged
    again = import_definitions(catalog, write(tmp_path, LINES))
    assert (again["added"], again["changed"]) == (0, 0)


def test_what_is_refused_names_the_line(catalog, tmp_path):
    for lines, message in (([{"select": "SELECT 1"}], "line 1: name a table"),
                           ([LINES[0], {"target": "a.b", "sql": "SELECT 1"}], "line 2: unknown field"),
                           ([{"target": "a.b"}], "select is the SQL"),
                           ([LINES[0], LINES[0]], "more than once")):
        with pytest.raises(CatalogError, match=message):
            import_definitions(catalog, write(tmp_path, lines))
    (tmp_path / "bad.jsonl").write_text("{not json\n")
    with pytest.raises(CatalogError, match="bad.jsonl line 1 is not JSON"):
        import_definitions(catalog, str(tmp_path / "bad.jsonl"))
    assert catalog.find(kind="table") and not [t for t in catalog.find(kind="table") if t["key"] == "table:a.b"]  # nothing was written


def test_the_command_line_imports_a_definitions_file(database, tmp_path, capsys):
    assert main(["--database-url", database, "import", "--sql-definitions", write(tmp_path, LINES)]) == 0
    assert "2 definitions" in capsys.readouterr().out


def test_tables_that_are_neither_defined_nor_registered_and_sql_not_traced_are_named(catalog, database, tmp_path, capsys):
    lines = [{"target": "hub.summary.a", "select": "SELECT org_id FROM hub.raw.shifts", "dependencies": ["hub.raw.missing"]},
             {"target": "hub.summary.b", "select": "SELEC garbage FROM"}]
    result = import_definitions(catalog, write(tmp_path, lines))
    assert result["unresolved"] == ["hub.raw.missing"] and result["untraced"] == ["hub.summary.b"]
    assert main(["--database-url", database, "import", "--sql-definitions", write(tmp_path, lines)]) == 0
    out = capsys.readouterr().out
    assert "registered by name only: hub.raw.missing" in out and "not traced" in out and "hub.summary.b" in out


@pytest.mark.parametrize("line", [{"target": "hub.s.a", "select": "SELECT 1 AS x", "columns": {"x": 5}},
                                  {"target": "hub.s.a", "select": "SELECT 1 AS x", "type": 7},
                                  {"target": "hub.s.a", "select": "SELECT 1 AS x", "dependencies": [3]}])
def test_a_malformed_line_is_refused_by_name(catalog, tmp_path, line):
    with pytest.raises(CatalogError, match="line 1"):
        import_definitions(catalog, write(tmp_path, [line]))


def test_the_system_follows_the_dialect_unless_named(catalog, tmp_path):
    import_definitions(catalog, write(tmp_path, [{"target": "w.s.t", "select": "SELECT 1 AS x"}]), dialect="postgres")
    assert catalog.get("w.s.t")["system"] == "postgres"
