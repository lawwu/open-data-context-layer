"""Lineage around an entry as a graph, with every edge, for the explorer."""
import pytest

from odcl import Catalog, CatalogError


@pytest.fixture
def catalog(database):
    with Catalog(database, actor="ana") as c:
        for t in ("raw.s.orders", "stage.s.orders", "mart.s.daily", "mart.s.weekly"):
            c.put("table", {"path": t})
        c.put("column", {"table": "raw.s.orders", "name": "amount"})
        c.put("column", {"table": "raw.s.orders", "name": "fee"})
        c.put("column", {"table": "stage.s.orders", "name": "net", "derived_from": ["raw.s.orders.amount", "raw.s.orders.fee"]})
        # a diamond: total reads amount directly and through net
        c.put("column", {"table": "mart.s.daily", "name": "total", "derived_from": ["stage.s.orders.net", "raw.s.orders.amount"]})
        c.put("column", {"table": "mart.s.weekly", "name": "total", "derived_from": ["mart.s.daily.total"]})
        c.put("project", {"name": "forecast", "description": "Weekly forecast"})
        c.use("forecast", ["mart.s.weekly.total"])
        yield c


def pairs(g):
    return {(e["from"].split(":", 1)[1], e["to"].split(":", 1)[1], e["kind"]) for e in g["edges"]}


def test_every_edge_is_kept_where_lineage_meets(catalog):
    g = catalog.graph("mart.s.daily.total")
    assert pairs(g) == {("raw.s.orders.amount", "stage.s.orders.net", "derived_from"), ("raw.s.orders.fee", "stage.s.orders.net", "derived_from"),
                        ("stage.s.orders.net", "mart.s.daily.total", "derived_from"), ("raw.s.orders.amount", "mart.s.daily.total", "derived_from"),
                        ("mart.s.daily.total", "mart.s.weekly.total", "derived_from")}
    place = {n["key"].split(":", 1)[1]: (n["side"], n["depth"]) for n in g["nodes"]}
    assert place["mart.s.daily.total"] == (0, 0) and place["raw.s.orders.amount"] == (-1, 1) and place["raw.s.orders.fee"] == (-1, 2)
    assert place["mart.s.weekly.total"] == (1, 1)


def test_depth_direction_projects_and_tables(catalog):
    assert {n["key"] for n in catalog.graph("mart.s.daily.total", direction="up", depth=1)["nodes"]} == {
        "column:mart.s.daily.total", "column:stage.s.orders.net", "column:raw.s.orders.amount"}
    with_projects = catalog.graph("mart.s.daily.total", direction="down", projects=True)
    assert ("mart.s.weekly.total", "forecast", "uses") in pairs(with_projects)
    tables = catalog.graph("mart.s.daily.total", level="table")
    assert tables["focus"] == "table:mart.s.daily"
    assert pairs(tables) == {("raw.s.orders", "stage.s.orders", "derived_from"), ("stage.s.orders", "mart.s.daily", "derived_from"),
                             ("raw.s.orders", "mart.s.daily", "derived_from"), ("mart.s.daily", "mart.s.weekly", "derived_from")}
    with pytest.raises(CatalogError, match="direction"):
        catalog.graph("mart.s.daily.total", direction="sideways")


def test_at_table_level_a_project_that_uses_columns_shows_on_their_table(catalog):
    g = catalog.graph("mart.s.daily", level="table", projects=True)
    assert ("mart.s.weekly", "forecast", "uses") in pairs(g)
