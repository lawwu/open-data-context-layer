"""What changed in an entry between two points in its history, labelled by how much it matters to what uses it."""
import time
from datetime import datetime, timezone

import pytest

from odcl import Catalog, CatalogError
from odcl.cli import main


@pytest.fixture
def catalog(database):
    with Catalog(database, actor="ana") as c:
        c.put("table", {"path": "core.sales.daily_totals", "description": "Totals by day"})
        for name, dtype in (("day", "DATE"), ("amount", "NUMERIC"), ("store_id", "INT64")):
            c.put("column", {"table": "core.sales.daily_totals", "name": name, "dtype": dtype})
        yield c


def by_key(result):
    return {(c["key"].rsplit(".", 1)[-1] if c["key"].startswith("column:") else "table", c["change"]): c["severity"] for c in result["changes"]}


def test_a_table_diff_labels_each_change_to_it_and_its_columns(catalog):
    before = catalog.get("core.sales.daily_totals.store_id")["revision"]  # any revision is a point in time: here, after the columns
    catalog.put("table", {"path": "core.sales.daily_totals", "description": "Net totals by day"})
    catalog.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "FLOAT64"})
    catalog.put("column", {"table": "core.sales.daily_totals", "name": "region", "dtype": "STRING"})
    catalog.put("column", {"table": "core.sales.daily_totals", "name": "store_id", "dtype": "INT64", "definition": "CAST(store AS INT64)"})
    catalog._write({**catalog._head("column:core.sales.daily_totals.day")["body"], "retired": True, "retired_by": "test", "links": []})

    result = catalog.diff("core.sales.daily_totals", since=before)
    assert by_key(result) == {("table", "description changed"): "cosmetic", ("amount", "type changed"): "breaking", ("region", "added"): "additive",
                              ("store_id", "definition changed"): "semantic", ("day", "retired"): "breaking"}
    assert result["severity"] == "breaking" and result["from"]["revision"] == catalog.diff("core.sales.daily_totals", until=before)["to"]["revision"]
    amount = next(c for c in result["changes"] if c["key"].endswith(".amount"))
    assert (amount["before"], amount["after"]) == ("NUMERIC", "FLOAT64")


def test_with_no_points_a_diff_is_the_latest_change_and_a_time_works_as_a_point(catalog):
    time.sleep(1.1)  # revisions are stamped to the second
    start = datetime.now(timezone.utc)
    time.sleep(1.1)
    catalog.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "NUMERIC", "description": "Net of refunds"})
    latest = catalog.diff("core.sales.daily_totals.amount")
    assert by_key(latest) == {("amount", "description changed"): "cosmetic"} and latest["severity"] == "cosmetic"
    assert catalog.diff("core.sales.daily_totals", since=start)["severity"] == "cosmetic"
    assert catalog.diff("core.sales.daily_totals", since=datetime.now(timezone.utc))["changes"] == []
    with pytest.raises(CatalogError, match="No revision"):
        catalog.diff("core.sales.daily_totals", since="rev_missing")


def test_a_type_stated_for_the_first_time_is_not_a_type_change(catalog):
    catalog.put("column", {"table": "core.sales.daily_totals", "name": "note"})
    catalog.put("column", {"table": "core.sales.daily_totals", "name": "note", "dtype": "STRING"})
    assert catalog.diff("core.sales.daily_totals.note")["severity"] == "cosmetic"


def test_what_a_project_used_says_how_much_it_changed_since(catalog, database, capsys):
    catalog.put("project", {"name": "forecast", "description": "Daily forecast"})
    catalog.use("forecast", ["core.sales.daily_totals.amount", "core.sales.daily_totals.store_id"])
    catalog.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "FLOAT64"})
    uses = {u["key"]: u for u in catalog.get("forecast")["uses"]}
    assert uses["column:core.sales.daily_totals.amount"]["severity"] == "breaking"
    assert uses["column:core.sales.daily_totals.store_id"]["severity"] is None
    assert catalog.overview()["changed_since_used"] == [{"project": "project:forecast", "key": "column:core.sales.daily_totals.amount", "severity": "breaking"}]

    assert main(["--database-url", database, "diff", "core.sales.daily_totals.amount"]) == 0
    out = capsys.readouterr().out
    assert "breaking" in out and "type changed: NUMERIC -> FLOAT64" in out


def test_a_used_table_is_compared_from_when_it_was_used_not_from_its_own_older_revision(catalog):
    catalog.put("project", {"name": "dash", "description": "Sales dashboard"})
    catalog.use("dash", ["core.sales.daily_totals"])  # the table's revision predates its columns
    catalog.put("table", {"path": "core.sales.daily_totals", "description": "Totals by day, net"})
    catalog.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "FLOAT64"})
    used = catalog.get("dash")["uses"][0]
    assert used["changed_since"] and used["severity"] == "breaking"  # the type change, not "every column added"


def test_a_used_table_whose_column_changed_is_changed_since_use(catalog):
    catalog.put("project", {"name": "board", "description": "Board pack"})
    catalog.use("board", ["core.sales.daily_totals"])
    assert catalog.overview()["changed_since_used"] == []
    catalog.put("column", {"table": "core.sales.daily_totals", "name": "store_id", "dtype": "STRING"})  # the table's own revision is unchanged
    assert catalog.overview()["changed_since_used"] == [{"project": "project:board", "key": "table:core.sales.daily_totals", "severity": "breaking"}]
    used = catalog.get("board")["uses"][0]
    assert used["changed_since"] and used["severity"] == "breaking"
