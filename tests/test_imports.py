"""One import session, whatever it reads from: all or nothing, one result shape, and what is gone is withdrawn."""
import pytest

from odcl import Catalog, CatalogError
from odcl.imports import FIELDS


def table(path, **source):
    return ("table", {"path": path, "source": {"shop": source}})


def column(path, name):
    return ("column", {"table": path, "name": name, "source": {"shop": {"position": 1}}})


def run(c, entries, **options):
    with c.importing("shop", **options) as imp:
        imp.extend(entries)
    return imp.result


def test_every_import_answers_the_same_fields(database):
    with Catalog(database, actor="ana") as c:
        with c.importing("shop", scope={"database": "shop"}) as imp:
            imp.add(*table("shop.public.orders", database="shop", schema="public"))
            imp.add(*column("shop.public.orders", "id"))
            imp.unresolved("column:shop.public.orders.store_id -> column:shop.ref.stores.id")
            imp.skip("shop.public.Odd Name: not a name the catalog keys")
        assert tuple(imp.result) == FIELDS
        assert imp.result == {"added": 2, "changed": 0, "unchanged": 0, "retired": [], "withdrawn": [],
                              "unresolved": ["column:shop.public.orders.store_id -> column:shop.ref.stores.id"],
                              "skipped": ["shop.public.Odd Name: not a name the catalog keys"]}


def test_what_is_gone_within_the_scope_is_retired_and_the_rest_is_left(database):
    with Catalog(database, actor="ana") as c:
        first = [table("shop.public.orders", database="shop", schema="public"), column("shop.public.orders", "id"), column("shop.public.orders", "note"),
                 table("shop.audit.log", database="shop", schema="audit"), table("other.public.t", database="other", schema="public")]
        run(c, first, scope={"database": ["shop", "other"]})
        again = run(c, [table("shop.public.orders", database="shop", schema="public"), column("shop.public.orders", "id")],
                    scope={"database": "shop", "schema": ["public"]})  # one schema of one database was read
        assert again["retired"] == ["column:shop.public.orders.note"] and (again["added"], again["changed"]) == (0, 0)
        assert "retired" not in c.get("shop.audit.log") and "retired" not in c.get("other.public.t")
        assert run(c, [], scope={"database": "shop", "schema": None})["retired"] == ["table:shop.audit.log", "table:shop.public.orders",
                                                                                       "column:shop.public.orders.id"]  # None: any schema


def test_naming_the_tables_read_withdraws_their_columns_and_never_a_table(database):
    with Catalog(database, actor="ana") as c:
        run(c, [table("shop.public.orders"), column("shop.public.orders", "id"), column("shop.public.orders", "note"), table("shop.public.kept")],
            tables=["shop.public.orders", "shop.public.kept"])
        again = run(c, [table("shop.public.orders"), column("shop.public.orders", "id")], tables=["Shop.Public.Orders"])
        assert again["retired"] == ["column:shop.public.orders.note"] and "retired" not in c.get("shop.public.kept")
        assert run(c, [], tables=["shop.public.orders"])["retired"] == ["column:shop.public.orders.id"]  # its table is not retired
        assert run(c, [])["retired"] == []  # no scope and no tables: nothing is withdrawn


def test_an_import_that_fails_registers_nothing(database):
    with Catalog(database, actor="ana") as c:
        with pytest.raises(CatalogError):
            with c.importing("shop") as imp:
                imp.add(*table("shop.public.orders"))
                imp.add("column", {"table": "shop.public.missing", "name": "id"})
        assert c.find("orders") == []
        with pytest.raises(RuntimeError):
            with c.importing("shop") as imp:
                imp.add(*table("shop.public.orders"))
                raise RuntimeError("the source went away")
        assert imp.result is None and c.find("orders") == []
