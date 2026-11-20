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
    assert counts == {"table": 5, "column": 19, "project": 0}
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
