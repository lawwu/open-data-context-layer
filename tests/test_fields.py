"""The field table: every field an entry can say is kept and cleared the way the table says."""
import pytest

from odcl import Catalog
from odcl.fields import FIELDS, of

WHERE = {"table": {"path": "core.sales.daily_totals"}, "column": {"table": "core.sales.daily_totals", "name": "net"},
         "project": {"name": "forecast", "description": "Daily forecast"}, "term": {"name": "net-revenue", "description": "After refunds"},
         "domain": {"name": "sales", "description": "Selling"}}
VALUES = {"derived_from": {"table": ["core.raw.sales"], "column": ["core.sales.daily_totals.amount"]}, "references": ["core.ref.stores.id"],
          "terms": ["gross"], "domain": "finance", "synonyms": ["NR"], "instances": ["t-*.sales.daily_totals"], "system": "bigquery", "layer": "gold",
          "owner": "ana", "type": "view", "dtype": "NUMERIC", "definition": "amount - fee", "repository": "repo", "status": "live", "pii": True,
          "data_category": "financial", "retention": "400d", "scrubbing": "hashed", "created_by": {"kind": "script", "path": "make.py"}}


@pytest.fixture
def catalog(database):
    with Catalog(database, actor="ana") as c:
        for path in ("core.raw.sales", "core.ref.stores", "core.sales.daily_totals"):
            c.put("table", {"path": path})
        c.put("column", {"table": "core.ref.stores", "name": "id"})
        c.put("column", {"table": "core.sales.daily_totals", "name": "amount"})
        c.put("domain", {"name": "finance", "description": "Money"})
        c.put("term", {"name": "gross", "description": "Before refunds"})
        yield c


def cases():
    return [pytest.param(kind, f, id=f"{kind}.{f.name}") for kind in WHERE for f in of(kind)]


def test_the_table_covers_every_field_once_per_kind():
    for kind in WHERE:
        names = [f.name for f in of(kind)]
        assert len(names) == len(set(names)), kind
    assert {f.name for f in FIELDS} == set(VALUES), "a field without a value to test it with"
    assert [f.name for f in FIELDS if f.relation or f.name == "instances"][:4] == ["derived_from", "references", "terms", "domain"]  # the order of links


@pytest.mark.parametrize("kind, field", cases())
def test_a_field_is_kept_and_cleared_as_the_table_says(catalog, kind, field):
    value = VALUES[field.name][kind] if field.name == "derived_from" else VALUES[field.name]
    said = lambda: field.said(catalog.history.entry(catalog.put(kind, WHERE[kind], merge=True)["key"])["body"])  # noqa: E731
    catalog.put(kind, {**WHERE[kind], field.name: value})
    first = said()
    assert first not in (None, [], ""), "the field was not stored"

    catalog.put(kind, WHERE[kind], merge=True)  # a merge that leaves it out keeps it
    assert said() == first
    catalog.put(kind, WHERE[kind])  # saying the whole entry without it keeps only what is said beside importers
    kept = said()
    assert (kept == first) if field.beside else (kept in (None, [], ""))

    catalog.put(kind, {**WHERE[kind], field.name: value})
    catalog.put(kind, {**WHERE[kind], field.name: None})  # saying it as None clears it, beside or not
    assert said() in (None, [], "")
