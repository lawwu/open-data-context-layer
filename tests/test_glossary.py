"""Glossary terms and domains: what a column means in the business's words, and which part of the business owns what."""
import pytest

from odcl import Catalog, CatalogError
from odcl.cli import main


@pytest.fixture
def catalog(database):
    with Catalog(database, actor="ana") as c:
        c.put("table", {"path": "core.sales.daily_totals"})
        c.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "NUMERIC"})
        c.put("project", {"name": "forecast", "description": "Daily forecast"})
        yield c


def test_columns_mean_terms_and_tables_projects_and_terms_belong_to_a_domain(catalog):
    catalog.put("domain", {"name": "sales", "description": "Selling, from quote to cash", "owner": "ana"})
    catalog.put("term", {"name": "net-revenue", "description": "Revenue after refunds and discounts", "synonyms": ["net sales", "NR"], "domain": "sales",
                         "status": "approved"})
    with pytest.raises(CatalogError, match="description"):
        catalog.put("term", {"name": "vague"})  # a term is its definition
    catalog.set_terms("core.sales.daily_totals.amount", ["net-revenue"])
    catalog.set_domain("core.sales.daily_totals", "sales")
    catalog.set_domain("forecast", "sales")

    term = catalog.get("term:net-revenue")
    assert [d["key"] for d in term["domain"]] == ["domain:sales"] and [m["key"] for m in term["meant_by"]] == ["column:core.sales.daily_totals.amount"]
    assert {m["key"] for m in catalog.get("domain:sales")["members"]} == {"table:core.sales.daily_totals", "project:forecast", "term:net-revenue"}
    assert [t["key"] for t in catalog.get("core.sales.daily_totals.amount")["terms"]] == ["term:net-revenue"]
    assert catalog.find("net sales", kind="term")[0]["key"] == "term:net-revenue"  # synonyms are searched
    assert catalog.glossary("sales") == [{"key": "term:net-revenue", "title": "net-revenue", "definition": "Revenue after refunds and discounts",
                                          "owner": None, "status": "approved", "synonyms": ["NR", "net sales"], "domain": "domain:sales", "meant_by": 1}]


def test_terms_and_domains_survive_re_registration_and_can_be_cleared(catalog):
    catalog.put("domain", {"name": "sales", "description": "Selling"})
    catalog.put("term", {"name": "gross", "description": "Before refunds"})
    catalog.set_terms("core.sales.daily_totals.amount", ["gross"])
    catalog.set_domain("core.sales.daily_totals", "sales")
    catalog.set_domain("forecast", "sales")
    catalog.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "NUMERIC", "description": "Amount"})  # as dbt would
    catalog.put("table", {"path": "core.sales.daily_totals", "description": "Totals"})
    catalog.put("project", {"name": "forecast", "description": "Daily forecast, revised"})
    assert [t["key"] for t in catalog.get("core.sales.daily_totals.amount")["terms"]] == ["term:gross"]
    assert [d["key"] for d in catalog.get("core.sales.daily_totals")["domain"]] == ["domain:sales"]
    assert [d["key"] for d in catalog.get("forecast")["domain"]] == ["domain:sales"]
    catalog.set_terms("core.sales.daily_totals.amount", [])
    catalog.set_domain("forecast", None)
    assert catalog.get("core.sales.daily_totals.amount")["terms"] == [] and catalog.get("forecast")["domain"] == []
    assert catalog.diff("core.sales.daily_totals.amount")["changes"][0]["change"] == "glossary terms changed"


def test_the_command_line_keeps_a_glossary(catalog, database, capsys):
    db = ["--database-url", database]
    assert main([*db, "domain", "add", "sales", "--description", "Selling"]) == 0
    assert main([*db, "term", "add", "net-revenue", "--definition", "Revenue after refunds", "--synonym", "net sales", "--domain", "sales"]) == 0
    assert main([*db, "means", "core.sales.daily_totals.amount", "net-revenue"]) == 0
    assert main([*db, "in-domain", "core.sales.daily_totals", "sales"]) == 0
    capsys.readouterr()
    assert main([*db, "glossary"]) == 0
    out = capsys.readouterr().out
    assert "net-revenue  [sales]  meant by 1" in out and "Revenue after refunds" in out and "also: net sales" in out
