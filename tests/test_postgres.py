"""A PostgreSQL database's tables, views, view lineage and foreign keys, read from its system catalog."""
import os
import uuid

import psycopg
import pytest

pytest.importorskip("sqlglot")
from odcl import Catalog  # noqa: E402
from odcl.cli import main  # noqa: E402
from odcl.postgres import import_database  # noqa: E402


@pytest.fixture
def shop(database):
    """Two schemas in the test database: `shop_…` with tables and views to read, and `ref_…`, which is not read."""
    base = os.environ["ODCL_TEST_DATABASE_URL"]
    shop, ref = f"shop_{uuid.uuid4().hex[:8]}", f"ref_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(base, autocommit=True) as db:
        db.execute(f"""
            CREATE SCHEMA {ref}; CREATE TABLE {ref}.stores (id int PRIMARY KEY);
            CREATE SCHEMA {shop};
            CREATE TABLE {shop}.customers (id int PRIMARY KEY, email text NOT NULL);
            COMMENT ON TABLE {shop}.customers IS 'Customers, one row each';
            COMMENT ON COLUMN {shop}.customers.email IS 'Login email';
            CREATE TABLE {shop}.orders (id int PRIMARY KEY, customer_id int REFERENCES {shop}.customers (id), store_id int REFERENCES {ref}.stores (id),
                                        amount numeric(10, 2), "CreatedAt" timestamptz);
            CREATE VIEW {shop}.order_totals AS
              WITH o AS (SELECT * FROM {shop}.orders) SELECT c.email, sum(o.amount) AS total, count(*) AS n
              FROM o JOIN {shop}.customers c ON c.id = o.customer_id GROUP BY c.email;
            CREATE VIEW {shop}.big_spenders AS SELECT email FROM {shop}.order_totals WHERE total > 100;
            CREATE MATERIALIZED VIEW {shop}.daily AS SELECT "CreatedAt"::date AS day, sum(amount) AS amount FROM {shop}.orders GROUP BY 1;
        """)
    try:
        yield base, shop, ref
    finally:
        with psycopg.connect(base, autocommit=True) as db:
            db.execute(f"DROP SCHEMA {shop} CASCADE; DROP SCHEMA {ref} CASCADE")


def test_tables_views_lineage_and_foreign_keys_are_read_from_the_system_catalog(database, shop):
    url, schema, ref = shop
    with Catalog(database, actor="ana") as c:
        result = import_database(c, url, schemas=[schema], database="shop")
        assert (result["relations"], result["views_traced"], result["foreign_keys"]) == (5, 3, 2)
        assert result["skipped"] == [f"foreign key column:shop.{schema}.orders.store_id -> column:shop.{ref}.stores.id: the referenced table was not read"]
        t = lambda name: f"shop.{schema}.{name}"

        customers = c.get(t("customers"))
        assert (customers["system"], customers["type"], customers["description"]) == ("postgres", "table", "Customers, one row each")
        assert "postgresql://" not in str(customers["source"]) and customers["source"]["postgres"]["schema"] == schema  # the URL is not kept
        email = c.get(f"{t('customers')}.email")
        assert (email["dtype"], email["description"], email["source"]["postgres"]["not_null"]) == ("text", "Login email", True)
        assert c.get(f"{t('orders')}.createdat")["dtype"] == "timestamp with time zone"  # a quoted name is keyed lower-cased

        assert [r["key"] for r in c.get(f"{t('orders')}.customer_id")["references"]] == [f"column:{t('customers')}.id"]
        assert [f["column"] for f in c.get(t("orders"))["foreign_keys"]] == [f"column:{t('orders')}.customer_id"]

        totals = c.get(t("order_totals"))
        assert totals["type"] == "view" and {d["key"] for d in totals["derived_from"]} == {f"table:{t('customers')}", f"table:{t('orders')}"}
        total = c.get(f"{t('order_totals')}.total")
        assert total["dtype"] == "numeric" and [d["key"] for d in total["derived_from"]] == [f"column:{t('orders')}.amount"]  # through the CTE's *
        assert "sum" in total["definition"].lower() and total["tags"] == []
        n = c.get(f"{t('order_totals')}.n")
        assert "lineage:partial" in n["tags"]  # a row count reads no column
        spender = c.get(f"{t('big_spenders')}.email")  # a view over a view, traced down to the table
        assert [d["key"] for d in spender["derived_from"]] == [f"column:{t('order_totals')}.email"]
        up = [x["key"] for x in c.lineage(f"{t('big_spenders')}.email", "up")["upstream"]]
        assert up == [f"column:{t('order_totals')}.email", f"column:{t('customers')}.email"]
        daily = c.get(f"{t('daily')}.amount")
        assert c.get(t("daily"))["type"] == "materialized view" and [d["key"] for d in daily["derived_from"]] == [f"column:{t('orders')}.amount"]

        again = import_database(c, url, schemas=[schema], database="shop")
        assert (again["added"], again["changed"]) == (0, 0)


def test_the_command_line_reads_a_database(database, shop, capsys):
    url, schema, _ = shop
    assert main(["--database-url", database, "import", "--postgres", url, "--schema", schema]) == 0
    out = capsys.readouterr().out
    dbname = psycopg.conninfo.conninfo_to_dict(url)["dbname"]
    assert f"5 relations from {dbname}" in out and "3 views traced, 2 foreign keys" in out and "skipped: foreign key" in out


@pytest.fixture
def awkward(database):
    """A schema with what a real database has and the shop does not: a partitioned table with a foreign key, a key
    on a column whose name the catalog does not key, two names that differ only in case, and a view over a table in
    a schema that is not read."""
    base = os.environ["ODCL_TEST_DATABASE_URL"]
    s, other = f"awk_{uuid.uuid4().hex[:8]}", f"oth_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(base, autocommit=True) as db:
        db.execute(f"""
            CREATE SCHEMA {other}; CREATE TABLE {other}.x (id int, v int);
            CREATE SCHEMA {s};
            CREATE TABLE {s}.parent (a int PRIMARY KEY);
            CREATE TABLE {s}.events (id int, d date, a int REFERENCES {s}.parent (a)) PARTITION BY RANGE (d);
            CREATE TABLE {s}.events_2026 PARTITION OF {s}.events FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
            CREATE TABLE {s}.odd (id int, "my col" int REFERENCES {s}.parent (a));
            CREATE TABLE {s}."Twin" (id int); CREATE TABLE {s}.twin (id int, extra int);
            CREATE VIEW {s}.cross_v AS SELECT x.v FROM {other}.x JOIN {s}.parent p ON p.a = x.id;
        """)
    try:
        yield base, s, other
    finally:
        with psycopg.connect(base, autocommit=True) as db:
            db.execute(f"DROP SCHEMA {s} CASCADE; DROP SCHEMA {other} CASCADE")


def test_partitions_odd_names_and_case_twins_are_skipped_and_said(database, awkward):
    url, s, other = awkward
    with Catalog(database, actor="ana") as c:
        result = import_database(c, url, schemas=[s], database="db")
        events = c.get(f"db.{s}.events.a")
        assert [r["key"] for r in events["references"]] == [f"column:db.{s}.parent.a"]  # the key, not its copy on each partition
        assert result["foreign_keys"] == 2
        assert any(f"odd.my col -> column:db.{s}.parent.a: its column was skipped" in x for x in result["skipped"])
        twins = [x for x in result["skipped"] if "once lower-cased" in x]
        assert len(twins) == 1 and [k["key"] for k in c.get(f"db.{s}.twin")["columns"]] in ([f"column:db.{s}.twin.id"],
                                                                                           [f"column:db.{s}.twin.extra", f"column:db.{s}.twin.id"])


def test_importing_a_view_over_a_schema_not_read_again_changes_nothing(database, awkward):
    url, s, other = awkward
    with Catalog(database, actor="ana") as c:
        first = import_database(c, url, schemas=[s], database="db")
        v = c.get(f"db.{s}.cross_v.v")
        assert [d["key"] for d in v["derived_from"]] == [f"column:db.{other}.x.v"]
        assert any(f"db.{other}.x: read by a view" in x for x in first["skipped"])
        again = import_database(c, url, schemas=[s], database="db")
        assert (again["added"], again["changed"]) == (0, 0) and again["skipped"] == first["skipped"]
        assert [d["key"] for d in c.get(f"db.{s}.cross_v.v")["derived_from"]] == [f"column:db.{other}.x.v"]
