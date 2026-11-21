"""Column lineage from SQL: every relation it names is a real table, never one of the query's own CTEs."""
import pytest

pytest.importorskip("sqlglot")
from odcl.lineage import column_parents, selected, tables  # noqa: E402

COUNTED_THROUGH_CTES = """WITH base AS (SELECT orgId, is_train FROM `p.d.shifts`),
w AS (SELECT *, TRUE AS flag FROM base),
stats AS (SELECT orgId, COUNT(*) AS org_shifts FROM w WHERE is_train GROUP BY orgId)
SELECT orgId, org_shifts FROM stats"""


def test_a_count_over_ctes_is_traced_to_the_table_the_ctes_read():
    # a row count has no input column, so it stays partial; its filter and grouping columns come from p.d.shifts, not `w`
    assert column_parents("org_shifts", COUNTED_THROUGH_CTES, "bigquery", {}) == (
        "org_shifts", [("p.d.shifts", "is_train"), ("p.d.shifts", "orgid")], False)


def test_a_value_through_nested_ctes_is_traced_to_its_real_column():
    sql = """WITH a AS (SELECT x, region FROM `p.d.t`), b AS (SELECT x * 2 AS y, region FROM a), c AS (SELECT * FROM b)
             SELECT y FROM c"""
    assert column_parents("y", sql, "bigquery", {}) == ("y", [("p.d.t", "x")], True)
    nested_count = """WITH a AS (SELECT x, region FROM `p.d.t`), b AS (SELECT * FROM a), c AS (SELECT * FROM b)
                      SELECT region, COUNT(*) AS n FROM c WHERE x > 0 GROUP BY region"""
    assert column_parents("n", nested_count, "bigquery", {}) == ("COUNT(*)", [("p.d.t", "region"), ("p.d.t", "x")], False)


def test_a_cte_named_like_the_last_part_of_a_real_table_is_followed_to_that_table():
    sql = """WITH shifts AS (SELECT orgId, is_train FROM `p.d.shifts`)
             SELECT orgId, COUNT(*) AS n FROM shifts WHERE is_train GROUP BY orgId"""
    assert column_parents("n", sql, "bigquery", {}) == ("COUNT(*)", [("p.d.shifts", "is_train"), ("p.d.shifts", "orgid")], False)


def test_a_count_over_one_table_is_traced_as_before():
    sql = "SELECT orgId, COUNT(*) AS n FROM `p.d.shifts` WHERE is_train GROUP BY orgId"
    assert column_parents("n", sql, "bigquery", {}) == ("COUNT(*)", [("p.d.shifts", "is_train"), ("p.d.shifts", "orgid")], False)


def test_a_cte_that_reads_no_table_yields_no_relation_rather_than_its_own_name():
    sql = """WITH seed AS (SELECT 1 AS k UNION ALL SELECT 2), counted AS (SELECT k, COUNT(*) AS n FROM seed GROUP BY k)
             SELECT n FROM counted"""
    expression, parents, complete = column_parents("n", sql, "bigquery", {})
    assert parents == [] and complete is False


def test_a_cte_defined_inside_a_subquery_is_followed_too():
    sql = """SELECT n FROM (WITH a AS (SELECT region, x FROM `p.d.t`) SELECT region, COUNT(*) AS n FROM a WHERE x > 0 GROUP BY region)"""
    assert column_parents("n", sql, "bigquery", {}) == ("n", [("p.d.t", "region"), ("p.d.t", "x")], False)


def test_table_names_in_any_case_match_the_lower_cased_keys_the_catalog_knows():
    known = {"proj": {"ds": {"orders": {"id": "unknown", "total": "unknown"}}}}
    for written in ("Proj.DS.Orders", "`Proj.DS.Orders`", "`Proj`.DS.ORDERS"):
        sql = f"SELECT * FROM {written}"
        assert tables(sql, "bigquery") == ["proj.ds.orders"]
        assert selected(sql, "bigquery", known) == ["id", "total"]  # the star expands to the columns the catalog has
        assert column_parents("total", sql, "bigquery", known)[1:] == ([("proj.ds.orders", "total")], True)
    through_cte = "WITH x AS (SELECT * FROM Proj.DS.Orders) SELECT Orders.total FROM X JOIN Proj.DS.Orders USING (id)"
    assert column_parents("total", through_cte, "bigquery", known) == ("Orders.total", [("proj.ds.orders", "total")], True)  # as written
