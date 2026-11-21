import json
from pathlib import Path

import pytest

from odcl import Catalog, CatalogError
from odcl.cli import main
from odcl.dbt import dbt_entries, web_url

DBT = Path(__file__).parent / "fixtures" / "dbt_medallion" / "target"
GOLD, SILVER, BRONZE = "churn.main_gold.account_features", "churn.main_silver.stg_accounts", "churn.bronze.accounts"


@pytest.fixture
def fathom(database):
    with Catalog(database, actor="ana") as f:
        f.apply(dbt_entries(DBT))
        yield f


def test_a_dbt_project_becomes_shared_tables_and_columns_with_lineage(fathom):
    counts = fathom.overview()["counts"]
    assert counts == {"table": 5, "column": 19, "project": 0, "document": 0}
    gold = fathom.get(GOLD)
    assert gold["layer"] == "gold" and gold["system"] == "duckdb" and gold["registered_by"] == "ana"
    assert [t["key"] for t in gold["derived_from"]] == ["table:churn.main_silver.stg_accounts", "table:churn.main_silver.stg_tickets"]
    assert gold["source"]["file"] == "models/gold/account_features.sql"
    tenure = fathom.get(f"column:{GOLD}.tenure_months")
    assert [c["key"] for c in tenure["derived_from"]] == [f"column:{SILVER}.opened_on"] and "DATE_DIFF" in tenure["definition"]
    up = [n["key"] for n in fathom.lineage(f"{GOLD}.tenure_months", "up")["upstream"]]
    assert up == [f"column:{SILVER}.opened_on", f"column:{BRONZE}.created_at"]  # gold -> silver -> bronze
    down = fathom.lineage(f"{BRONZE}.plan", "down")["downstream"]
    assert [n["key"] for n in down] == [f"column:{SILVER}.plan_code", f"column:{GOLD}.plan_tier"] and [n["depth"] for n in down] == [1, 2]
    counted = fathom.get(f"column:{GOLD}.support_tickets_90d")  # a row count has no input column: traced to its filters, and says so
    assert counted["tags"] == ["lineage:partial"] and counted["derived_from"]


def test_importing_again_changes_nothing_and_a_changed_table_does_not_ripple(fathom):
    assert fathom.apply(dbt_entries(DBT)) == {"added": 0, "changed": 0, "unchanged": 24}
    before = fathom.get(SILVER)["revision"]
    changed = fathom.put("table", {"path": BRONZE, "description": "Accounts, as the billing system exports them", "layer": "bronze"})
    assert changed["change"] == "changed" and changed["registered_by"] == "ana"
    assert fathom.get(SILVER)["revision"] == before  # what is built from it keeps its revision ...
    assert fathom.get(SILVER)["derived_from"][0]["changed_since"]  # ... and says its input changed
    assert len(fathom.get(BRONZE)["history"]) == 2


def test_selecting_a_model_reads_only_what_it_is_built_from():
    entries = dbt_entries(DBT, ["stg_tickets"])
    assert sorted(p["path"] for k, p in entries if k == "table") == ["churn.bronze.tickets", "churn.main_silver.stg_tickets"]
    with pytest.raises(CatalogError, match="No dbt model named nope"):
        dbt_entries(DBT, ["nope"])


def test_which_projects_use_this_column_and_what_did_this_project_use(fathom):
    fathom.put("project", {"name": "churn-model", "description": "Predict 90-day churn", "owner": "ben"})
    fathom.put("project", {"name": "billing-audit", "description": "Reconcile plans against invoices"})
    fathom.put("project", {"name": "support-dashboard", "description": "Ticket volumes"})
    fathom.use("churn-model", [f"{GOLD}.plan_tier", f"{GOLD}.tenure_months"], "training features")
    fathom.use("billing-audit", [f"column:{BRONZE}.plan"], "source of truth for plan")
    fathom.use("support-dashboard", [f"table:{SILVER}"])

    used = {p["project"]: p for p in fathom.used_by(f"{BRONZE}.plan")["projects"]}
    assert used["project:billing-audit"]["how"] == "direct" and used["project:billing-audit"]["purpose"] == "source of truth for plan"
    assert used["project:churn-model"]["how"] == "downstream" and used["project:churn-model"]["through"] == f"column:{GOLD}.plan_tier"
    assert used["project:support-dashboard"]["how"] == "downstream"  # it uses the silver table that holds plan_code
    assert list(used) == ["project:billing-audit", "project:churn-model", "project:support-dashboard"]  # strongest first
    assert [p["how"] for p in fathom.used_by(f"table:{GOLD}")["projects"]] == ["column"]
    assert [p["how"] for p in fathom.used_by(f"{SILVER}.opened_on")["projects"]] == ["table", "downstream"]

    mine = fathom.uses("churn-model", upstream=True)
    assert [u["key"] for u in mine["uses"]] == [f"column:{GOLD}.plan_tier", f"column:{GOLD}.tenure_months"]
    assert f"column:{BRONZE}.plan" in [n["key"] for n in mine["built_from"]] and mine["owner"] == "ben"
    assert fathom.find("plan tier")[0]["used_by"] == 1

    # the plan column is redefined after the project used it: the project is told, until it records use again
    fathom.put("column", {"table": GOLD, "name": "plan_tier", "description": "Plan tier, with trials counted as free", "derived_from": [f"column:{SILVER}.plan_code"]})
    assert fathom.overview()["changed_since_used"] == [{"project": "project:churn-model", "key": f"column:{GOLD}.plan_tier"}]
    assert [u["changed_since"] for u in fathom.uses("churn-model")["uses"]] == [True, False]
    fathom.use("churn-model", [f"{GOLD}.plan_tier"])
    assert fathom.overview()["changed_since_used"] == []
    assert fathom.uses("churn-model")["uses"][0]["purpose"] == "training features"  # recording use again keeps what it was for
    described = fathom.put("project", {"name": "churn-model", "description": "Predict churn in 90 days", "owner": "ben"})
    assert described["change"] == "changed" and len(fathom.uses("churn-model")["uses"]) == 2  # describing a project keeps what it used


def test_finding_data_by_words_kind_and_layer(fathom):
    assert [r["key"] for r in fathom.find("tenure")] == [f"column:{GOLD}.tenure_months"]
    assert fathom.find("ten")[0]["key"] == f"column:{GOLD}.tenure_months"  # a word's beginning is enough
    assert {r["kind"] for r in fathom.find("accounts", kind="table")} == {"table"}
    assert {r["layer"] for r in fathom.find(layer="bronze")} == {"bronze"} and len(fathom.find(layer="bronze")) == 9
    assert fathom.find("nothing-like-this") == [] and fathom.find('quote " and ) paren') == []
    assert len(fathom.find(limit=3)) == 3


def test_an_importer_adds_what_it_knows_without_erasing_what_another_said(fathom):
    """BigQuery knows a column's type; Dataform knows how it is computed. Each import keeps the other's fields."""
    bigquery = [("table", {"path": "p.d.t", "description": "Shift inputs", "system": "bigquery", "source": {"bigquery": {"location": "US"}}}),
                ("column", {"table": "p.d.t", "name": "x", "dtype": "FLOAT", "source": {"bigquery": {"mode": "NULLABLE"}}})]
    assert fathom.apply(bigquery, merge=True) == {"added": 2, "changed": 0, "unchanged": 0}
    fathom.apply([("table", {"path": "p.d.raw"}), ("column", {"table": "p.d.raw", "name": "m"})], merge=True)
    dataform = [("column", {"table": "p.d.t", "name": "x", "definition": "m / 60", "derived_from": ["column:p.d.raw.m"], "tags": ["lineage:partial"],
                            "source": {"dataform": {"file": "definitions/t.sqlx"}}})]
    assert fathom.apply(dataform, merge=True) == {"added": 0, "changed": 1, "unchanged": 0}

    column = fathom.get("column:p.d.t.x")
    assert (column["dtype"], column["definition"], column["tags"]) == ("FLOAT", "m / 60", ["lineage:partial"])
    assert column["source"] == {"bigquery": {"mode": "NULLABLE"}, "dataform": {"file": "definitions/t.sqlx", "tags": ["lineage:partial"]}}
    assert [c["key"] for c in column["derived_from"]] == ["column:p.d.raw.m"]
    assert fathom.get("table:p.d.t")["description"] == "Shift inputs"
    assert fathom.apply(bigquery, merge=True) == {"added": 0, "changed": 0, "unchanged": 2}  # each import again changes nothing
    assert fathom.apply(dataform, merge=True) == {"added": 0, "changed": 0, "unchanged": 1}
    fathom.apply([("column", {"table": "p.d.t", "name": "x", "tags": [], "source": {"dataform": {"file": "definitions/t.sqlx"}}})], merge=True)
    assert fathom.get("column:p.d.t.x")["tags"] == []  # what an importer says outright still replaces its own earlier word

    assert fathom.put("column", {"table": "p.d.t", "name": "x"})["dtype"] is None  # a plain registration says everything


def test_what_is_refused(fathom):
    for kind, payload, code in (("table", {"path": "Has Spaces"}, "path"), ("column", {"table": "nowhere.t", "name": "x"}, "not_found"),
                                ("project", {"name": "p"}, "description"), ("view", {}, "kind"),
                                ("table", {"path": "a.b", "derived_from": ["table:a.b"]}, "not_found")):
        with pytest.raises(CatalogError) as error:
            fathom.put(kind, payload)
        assert error.value.code == code
    fathom.put("table", {"path": "a.b"})
    fathom.put("table", {"path": "a.b.c"})
    fathom.put("column", {"table": "a.b.c", "name": "x"})
    assert fathom.get("table:a.b")["columns"] == []  # a.b.c's column is not a.b's
    fathom.put("project", {"name": "a.b", "description": "shares a name with a table"})
    with pytest.raises(CatalogError) as error:
        fathom.get("a.b")
    assert error.value.code == "ambiguous"
    with pytest.raises(CatalogError) as error:
        fathom.use("a.b", ["project:a.b"])  # a project uses data, not projects
    assert error.value.code == "not_found"


def test_the_command_line_end_to_end(empty_database, capsys):
    db = ["--database-url", empty_database, "--as", "ana"]
    assert main([*db, "overview"]) == 1 and "Run `odcl migrate`" in capsys.readouterr().err  # nothing works before the tables exist
    assert main([*db, "migrate"]) == 0 and "applied 0001_catalog" in capsys.readouterr().out
    assert main([*db, "migrate"]) == 0 and "up to date" in capsys.readouterr().out  # migrations apply once
    assert main([*db, "import", "--dbt", str(DBT)]) == 0
    assert "24 added" in capsys.readouterr().out
    assert main([*db, "add", "project", "churn-model", "--description", "Predict churn"]) == 0
    assert main([*db, "use", "churn-model", f"{GOLD}.plan_tier", "--for", "a feature"]) == 0
    capsys.readouterr()
    assert main([*db, "used-by", f"{BRONZE}.plan"]) == 0
    assert "project:churn-model  downstream through" in capsys.readouterr().out
    assert main([*db, "--json", "uses", "churn-model"]) == 0
    assert json.loads(capsys.readouterr().out)["uses"][0]["purpose"] == "a feature"
    assert main([*db, "find", "plan"]) == 0 and "used by 1" in capsys.readouterr().out
    assert main([*db, "lineage", f"{GOLD}.plan_tier", "--direction", "up"]) == 0 and "upstream:" in capsys.readouterr().out
    assert main([*db, "show", "nothing.here"]) == 1 and "Nothing registered" in capsys.readouterr().err


def test_a_git_remote_becomes_a_web_address_without_credentials():
    assert web_url("git@github.com:acme/data.git") == "https://github.com/acme/data"
    assert web_url("https://someone@github.com/acme/data.git") == "https://github.com/acme/data"
    assert web_url("/local/path") is None


def test_the_table_list_counts_columns_and_users_and_filters_by_layer_and_system(fathom):
    fathom.put("project", {"name": "churn-model", "description": "Predict churn"})
    fathom.use("churn-model", [f"{GOLD}.plan_tier"])
    tables = {t["key"]: t for t in fathom.tables()}
    assert len(tables) == 5
    gold = tables[f"table:{GOLD}"]
    assert gold["layer"] == "gold" and gold["system"] == "duckdb" and gold["columns"] == len(fathom.get(GOLD)["columns"]) and gold["used_by"] == 1
    assert tables[f"table:{BRONZE}"]["used_by"] == 1  # through what is built from it
    assert gold["changed"] == fathom.get(GOLD)["at"]
    assert [t["key"] for t in fathom.tables(layer="bronze")] == ["table:churn.bronze.accounts", "table:churn.bronze.tickets"]
    assert fathom.tables(system="bigquery") == []


def test_listed_user_counts_match_what_used_by_finds_for_each_entry(fathom):
    """The lists count users for every row at once; each count must agree with asking `used_by` of that one entry."""
    for name in ("churn-model", "billing-audit", "support-dashboard"):
        fathom.put("project", {"name": name, "description": name})
    fathom.use("churn-model", [f"column:{GOLD}.plan_tier", f"column:{GOLD}.tenure_months"])
    fathom.use("billing-audit", [f"column:{BRONZE}.plan"])
    fathom.use("support-dashboard", [f"table:{SILVER}"])
    listed = fathom.tables() + fathom.find("", "column", limit=2000)
    assert any(e["used_by"] > 1 for e in listed)
    for entry in listed:
        assert entry["used_by"] == len(fathom.used_by(entry["key"])["projects"]), entry["key"]


def test_a_large_import_holds_a_bounded_number_of_locks(database):
    from odcl.store import ENTRY_LOCKS
    held = []

    def entries():
        yield ("table", {"path": "big.d.t"})
        for i in range(3000):
            yield ("column", {"table": "big.d.t", "name": f"c{i}"})
        held.append(catalog.db.execute("SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()").fetchone()["n"])

    with Catalog(database, actor="ana") as catalog:
        assert catalog.apply(entries())["added"] == 3001
        assert 0 < held[0] <= ENTRY_LOCKS  # one lock per entry would be 3,001, past PostgreSQL's default lock table


def test_under_merge_each_importer_replaces_only_its_own_tags(database):
    with Catalog(database, actor="ana") as c:
        c.put("table", {"path": "p.d.t", "tags": ["reviewed"]})  # a person's tag
        c.put("table", {"path": "p.d.t", "tags": ["lineage:partial"], "source": {"dataform": {"file": "t.sqlx"}}}, merge=True)
        c.put("table", {"path": "p.d.t", "tags": ["tenant"], "source": {"datahub": {"urn": "x"}}}, merge=True)
        assert c.get("p.d.t")["tags"] == ["lineage:partial", "reviewed", "tenant"]
        c.put("table", {"path": "p.d.t", "tags": [], "source": {"dataform": {"file": "t.sqlx"}}}, merge=True)  # lineage resolved now
        said = c.get("p.d.t")
        assert said["tags"] == ["reviewed", "tenant"] and said["source"]["dataform"]["tags"] == [] and said["source"]["datahub"]["tags"] == ["tenant"]
        c.put("table", {"path": "p.d.t", "source": {"datahub": {"urn": "x"}}}, merge=True)  # sending no tags keeps them
        assert c.get("p.d.t")["tags"] == ["reviewed", "tenant"]

        # an entry an importer wrote before tags had owners: its next import takes them over, then tags add up again
        c.put("table", {"path": "p.d.old", "tags": ["a", "b"], "source": {"dataform": {"file": "old.sqlx"}}})
        c.put("table", {"path": "p.d.old", "tags": ["a"], "source": {"dataform": {"file": "old.sqlx"}}}, merge=True)
        assert c.get("p.d.old")["tags"] == ["a"]


def test_privacy_fields_are_said_kept_inherited_and_exported(fathom, database, tmp_path, capsys):
    accounts = fathom.get(BRONZE)
    fathom.set_privacy([BRONZE], data_category="customer", retention="400d")
    fathom.set_privacy([f"{BRONZE}.account_id"], pii=True, scrubbing="hashed")
    kept = fathom.get(BRONZE)
    assert (kept["data_category"], kept["retention"], kept["description"], kept["layer"]) == ("customer", "400d", accounts["description"], "bronze")
    assert fathom.get(f"{BRONZE}.account_id")["pii"] is True
    assert fathom.apply(dbt_entries(DBT), merge=True)["changed"] == 0  # an import that says nothing of privacy keeps it

    columns = {c["column"]: c for c in fathom.privacy(table=BRONZE)}
    assert columns["account_id"] | {} == {**columns["account_id"], "pii": True, "scrubbing": "hashed", "data_category": "customer",
                                          "retention": "400d", "inherited": ["data_category", "retention"]}
    assert columns["plan"]["pii"] is None  # not said is not "no"
    assert [c["key"] for c in fathom.privacy(pii_only=True)] == [f"column:{BRONZE}.account_id"]
    assert fathom.find("pii")[0]["key"] == f"column:{BRONZE}.account_id"
    with pytest.raises(CatalogError, match="pii is yes or no"):
        fathom.set_privacy([BRONZE], pii="maybe")

    db = ["--database-url", database]
    assert main([*db, "privacy", "set", f"{BRONZE}.plan", "--pii", "no", "--data-category", "billing"]) == 0
    capsys.readouterr()
    assert main([*db, "privacy", "export", "--table", BRONZE]) == 0
    rows = capsys.readouterr().out.splitlines()
    assert rows[0].startswith("key,table,column,dtype,pii,scrubbing,data_category,retention")
    assert f"column:{BRONZE}.plan,table:{BRONZE},plan," in rows[-1] and ",no,,billing,400d," in rows[-1]


def test_dbt_meta_says_what_is_personal_data(database, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    manifest = json.loads((DBT / "manifest.json").read_text())
    accounts = manifest["sources"]["source.churn.bronze.accounts"]
    accounts["meta"] |= {"data_category": "customer", "retention": "7 years"}
    accounts["columns"]["account_id"]["meta"] = {"privacy": {"pii": "yes", "scrubbing": "hashed"}}
    (target / "manifest.json").write_text(json.dumps(manifest))
    with Catalog(database, actor="ana") as c:
        c.apply(dbt_entries(target))
        assert (c.get(BRONZE)["data_category"], c.get(BRONZE)["retention"]) == ("customer", "7 years")
        assert (c.get(f"{BRONZE}.account_id")["pii"], c.get(f"{BRONZE}.account_id")["scrubbing"]) == (True, "hashed")
        assert "pii" not in c.get(f"{BRONZE}.plan")  # only what is said is kept


def test_copies_of_a_definition_are_linked_by_pattern_and_lineage_and_users_go_through_them(database, capsys):
    definition, copy = "core.sales.daily_totals", "t-1.sales.daily_totals"
    with Catalog(database, actor="ana") as c:
        c.put("table", {"path": "core.ref.discounts"})
        c.put("column", {"table": "core.ref.discounts", "name": "discounted"})
        c.put("table", {"path": definition, "derived_from": ["table:core.ref.discounts"]})
        c.put("column", {"table": definition, "name": "net_total", "definition": "SUM(IF(discounted, amount, 0))", "derived_from": ["column:core.ref.discounts.discounted"]})
        for tenant in ("t-1", "t-2"):
            c.put("table", {"path": f"{tenant}.sales.daily_totals", "system": "bigquery"})
            c.put("column", {"table": f"{tenant}.sales.daily_totals", "name": "net_total", "dtype": "FLOAT64"})
        c.put("project", {"name": "revenue-model", "description": "Revenue forecast"})
        c.use("revenue-model", [f"column:{copy}.net_total"])
        assert c.used_by(f"{definition}.net_total")["projects"] == []  # not connected yet

        linked = c.set_instances(definition, ["t-*.sales.daily_totals"])
        assert [link["to"] for link in linked["links"] if link["relation"] == "instance_of"] == [f"table:{copy}", "table:t-2.sales.daily_totals"]
        [user] = c.used_by(f"{definition}.net_total")["projects"]
        assert (user["project"], user["how"], user["through"]) == ("project:revenue-model", "downstream", f"column:{copy}.net_total")
        assert {t["key"]: t["used_by"] for t in c.tables()}[f"table:{definition}"] == 1
        up = [n["key"] for n in c.lineage(f"{copy}.net_total", "up")["upstream"]]
        assert up == [f"column:{definition}.net_total", "column:core.ref.discounts.discounted"]  # through the copy to the definition and its input
        assert "column:core.ref.discounts.discounted" in [n["key"] for n in c.uses("revenue-model", upstream=True)["built_from"]]

        c.put("table", {"path": "t-3.sales.daily_totals"})  # a tenant added later is linked when it is registered
        c.put("column", {"table": "t-3.sales.daily_totals", "name": "net_total"})
        shown = c.get(definition)
        assert [t["key"] for t in shown["copies"]] == [f"table:{copy}", "table:t-2.sales.daily_totals", "table:t-3.sales.daily_totals"]
        assert [t["key"] for t in c.get("t-3.sales.daily_totals")["copy_of"]] == [f"table:{definition}"]
        assert "column:t-3.sales.daily_totals.net_total" in [n["key"] for n in c.lineage(f"{definition}.net_total", "down")["downstream"]]
        assert c.put("table", {"path": definition, "description": "Net totals"}, merge=True)["instances"] == ["t-*.sales.daily_totals"]  # kept

        c.set_instances(definition, [])
        assert c.get(definition)["copies"] == [] and c.used_by(f"{definition}.net_total")["projects"] == []
    assert main(["--database-url", database, "instances", definition, "t-*.sales.daily_totals"]) == 0
    assert "copy: table:t-3.sales.daily_totals" in capsys.readouterr().out


def test_a_column_references_the_column_its_values_are_drawn_from(database, capsys):
    with Catalog(database, actor="ana") as c:
        for table, columns in (("shop.public.customers", ["id", "email"]), ("shop.public.orders", ["id", "customer_id"])):
            c.put("table", {"path": table})
            for column in columns:
                c.put("column", {"table": table, "name": column})
        c.put("column", {"table": "shop.public.orders", "name": "customer_id", "references": ["shop.public.customers.id"]})
        fk = c.get("shop.public.orders.customer_id")
        assert [r["key"] for r in fk["references"]] == ["column:shop.public.customers.id"] and fk["derived_from"] == []  # not lineage
        assert [r["key"] for r in c.get("shop.public.customers.id")["referenced_by"]] == ["column:shop.public.orders.customer_id"]
        orders, customers = c.get("shop.public.orders"), c.get("shop.public.customers")
        assert orders["foreign_keys"] == [{"column": "column:shop.public.orders.customer_id", "references": "column:shop.public.customers.id",
                                           "table": "table:shop.public.customers", "changed_since": False}]
        assert customers["referenced_by"] == [{"column": "column:shop.public.orders.customer_id", "table": "table:shop.public.orders",
                                               "references": "column:shop.public.customers.id"}]
        assert c.lineage("shop.public.customers.id", "down")["downstream"] == []
        c.put("column", {"table": "shop.public.orders", "name": "customer_id", "description": "Who ordered"}, merge=True)
        assert [r["key"] for r in c.get("shop.public.orders.customer_id")["references"]] == ["column:shop.public.customers.id"]  # kept
        with pytest.raises(CatalogError, match="Nothing registered"):
            c.set_references("shop.public.orders.id", ["shop.public.nowhere.id"])
        c.set_references("shop.public.orders.customer_id", [])
        assert c.get("shop.public.orders")["foreign_keys"] == []
    assert main(["--database-url", database, "references", "shop.public.orders.customer_id", "shop.public.customers.id"]) == 0
    assert "references column:shop.public.customers.id" in capsys.readouterr().out
