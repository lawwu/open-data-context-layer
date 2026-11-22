"""Reading the catalog's history: entries, their revisions, and what each revision pinned."""
import time
from datetime import datetime, timedelta, timezone

import pytest

from odcl import Catalog


@pytest.fixture
def catalog(database):
    with Catalog(database, actor="ana") as c:
        c.put("table", {"path": "core.sales.daily_totals"})
        c.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "NUMERIC"})
        c.put("column", {"table": "core.sales.daily_totals", "name": "day", "dtype": "DATE"})
        yield c


AMOUNT, TABLE = "column:core.sales.daily_totals.amount", "table:core.sales.daily_totals"


def test_entries_as_they_are_now(catalog):
    h = catalog.history
    assert h.entry(AMOUNT)["body"]["dtype"] == "NUMERIC" and h.entry("table:no.such.table") is None
    assert h.head(AMOUNT) == h.entry(AMOUNT)["id"] and h.head("table:no.such.table") is None
    assert set(h.entries([AMOUNT, TABLE, "table:no.such.table"])) == {AMOUNT, TABLE}
    assert h.known([AMOUNT, "column:core.sales.daily_totals.gone"]) == {AMOUNT}
    assert h.keys(["table"]) == [TABLE]
    assert [r["body"]["name"] for r in h.columns(TABLE)] == ["amount", "day"]
    catalog._write({**h.entry(AMOUNT)["body"], "retired": True, "links": []})
    assert [r["body"]["name"] for r in h.columns(TABLE, retired=False)] == ["day"] and len(h.columns(TABLE)) == 2


def test_revisions_and_points_in_their_order(catalog):
    h = catalog.history
    first = h.head(AMOUNT)
    start = h.now()
    time.sleep(1.1)  # revisions are stamped to the second
    between = datetime.now(timezone.utc)
    time.sleep(1.1)
    second = catalog.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "FLOAT64"})["revision"]
    assert [r["id"] for r in h.revisions(AMOUNT)] == [first, second]
    assert h.revision(first, AMOUNT)["body"]["dtype"] == "NUMERIC" and h.revision(first, TABLE) is None and h.revision("rev_missing") is None
    assert h.revision_at(AMOUNT, between) == first and h.revision_at(AMOUNT, datetime.now(timezone.utc)) == second
    assert h.revision_at(AMOUNT, between - timedelta(days=1)) is None
    assert h.revision_with(AMOUNT, "dtype", "NUMERIC") == first and h.revision_with(AMOUNT, "dtype", "TEXT") is None
    assert h.point(first) < h.point(second) == h.now() and h.point("rev_missing") is None
    assert h.point_at(between) == start and h.point_at(between - timedelta(days=1)) == 0
    assert h.newest_touching(TABLE) == second and h.newest_touching(AMOUNT) == second
    past = h.column_revisions(TABLE, start)
    assert set(past) == {AMOUNT, "column:core.sales.daily_totals.day"} and [r["id"] for r in past[AMOUNT]] == [first]


def test_what_a_revision_pinned_and_when_a_link_began(catalog):
    h = catalog.history
    catalog.put("column", {"table": "core.sales.daily_totals", "name": "net", "derived_from": [AMOUNT]})
    pinned = h.head(AMOUNT)
    catalog.put("project", {"name": "forecast", "description": "Daily forecast"})
    began = catalog.use("forecast", [AMOUNT])["revision"]
    catalog.put("project", {"name": "forecast", "description": "Daily forecast, revised"})  # a later revision pins the same
    catalog.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "FLOAT64"})
    [link] = h.links(h.head("column:core.sales.daily_totals.net"), "derived_from")
    assert (link["target_key"], link["target_revision"]) == (AMOUNT, pinned) and link["head_revision"] == h.head(AMOUNT) != pinned
    assert h.link_started("project:forecast", "uses", AMOUNT, pinned) == began
    assert h.link_started("project:forecast", "uses", AMOUNT, h.head(AMOUNT)) is None
