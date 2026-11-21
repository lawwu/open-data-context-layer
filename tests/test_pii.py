"""Proposed privacy fields, for a person to confirm before anything is recorded."""
import json

import pytest

from odcl import Catalog, CatalogError, pii
from odcl.cli import main


@pytest.fixture
def catalog(database):
    with Catalog(database, actor="ana") as c:
        c.put("table", {"path": "crm.core.customers", "description": "One row per customer"})
        c.put("table", {"path": "shop.core.products"})
        for table, name, description in (("crm.core.customers", "email", None), ("crm.core.customers", "name", None),
                                         ("crm.core.customers", "date_of_birth", None), ("crm.core.customers", "region", None),
                                         ("crm.core.customers", "notes", "Free text, may hold a phone number"),
                                         ("crm.core.customers", "customer_id", None), ("shop.core.products", "name", None),
                                         ("shop.core.products", "price", None)):
            c.put("column", {"table": table, "name": name, "description": description})
        yield c


def proposed(found):
    return {".".join(p["key"].split(".")[-2:]): (p["data_category"], p["confidence"]) for p in found}


def test_columns_are_proposed_from_their_names_terms_and_descriptions(catalog):
    found = proposed(pii.propose(catalog))
    assert found == {"customers.email": ("contact", "high"), "customers.name": ("identity", "medium"),  # a name on a table of people
                     "customers.date_of_birth": ("identity", "high"), "customers.notes": ("contact", "medium"),
                     "customers.customer_id": ("identifier", "medium"), "products.name": ("identity", "low")}
    assert proposed(pii.propose(catalog, minimum="medium")).keys() == found.keys() - {"products.name"}
    catalog.put("term", {"name": "home-location", "description": "Where a customer lives", "synonyms": ["home address"]})
    catalog.set_terms("crm.core.customers.region", ["home-location"])
    assert proposed(pii.propose(catalog, table="crm.core.customers"))["customers.region"] == ("contact", "low")


def test_columns_that_already_say_are_left_out_unless_asked(catalog):
    catalog.set_privacy(["crm.core.customers.email"], pii=True, data_category="contact")
    assert "customers.email" not in proposed(pii.propose(catalog))
    assert "customers.email" in proposed(pii.propose(catalog, everything=True))


def test_a_reviewed_file_records_only_what_was_accepted(catalog, tmp_path, database, capsys):
    review = tmp_path / "review.jsonl"
    assert main(["--database-url", database, "privacy", "propose", "--min-confidence", "medium", "--write", str(review)]) == 0
    assert "odcl privacy apply" in capsys.readouterr().out
    lines = [json.loads(line) for line in review.read_text().splitlines()]
    for line in lines:
        line["accept"] = line["key"].endswith((".email", ".date_of_birth"))
        if line["key"].endswith(".date_of_birth"):
            line["data_category"] = "identity_sensitive"  # the reviewer changes what is wrong
    review.write_text("".join(json.dumps(line) + "\n" for line in lines))
    assert main(["--database-url", database, "privacy", "apply", str(review)]) == 0
    assert "2 recorded" in capsys.readouterr().out
    assert catalog.get("crm.core.customers.date_of_birth")["data_category"] == "identity_sensitive"
    assert catalog.get("crm.core.customers.email")["pii"] is True and "pii" not in catalog.get("crm.core.customers.name")

    review.write_text(json.dumps({"key": "crm.core.customers.name", "accept": True, "pii": "maybe"}) + "\n")
    with pytest.raises(CatalogError, match="pii is true or false"):
        pii.apply_review(catalog, str(review))
