"""Data-quality results, pinned to the revision of the entry that was current when each test ran."""
import json
import time
from pathlib import Path

import pytest

from odcl import Catalog, CatalogError, quality
from odcl.cli import main
from odcl.dbt import dbt_entries

DBT = Path(__file__).parent / "fixtures" / "dbt_medallion" / "target"


@pytest.fixture
def catalog(database):
    with Catalog(database, actor="ana") as c:
        c.put("table", {"path": "core.sales.daily_totals"})
        c.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "NUMERIC"})
        yield c


def lines(tmp_path, rows):
    path = tmp_path / "results.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return str(path)


def test_results_are_pinned_kept_once_and_unknown_entries_are_named(catalog, tmp_path, database, capsys):
    first = catalog.get("core.sales.daily_totals.amount")["revision"]
    time.sleep(1.1)  # revisions are stamped to the second
    rows = [{"entry": "core.sales.daily_totals.amount", "test": "not_negative", "status": "fail", "failures": 4, "observed_at": "2099-01-01T00:00:00Z",
             "run": "r1", "kind": "expression"},
            {"entry": "core.sales.daily_totals", "test": "row_count", "status": "pass", "observed_at": "2099-01-01T00:00:00Z", "run": "r1"},
            {"entry": "core.sales.gone", "test": "x", "status": "pass", "observed_at": "2099-01-01T00:00:00Z", "run": "r1"}]
    assert quality.record(catalog, rows, "great_expectations") == {"added": 2, "unchanged": 0, "unresolved": ["core.sales.gone"]}
    assert quality.record(catalog, rows, "great_expectations")["added"] == 0  # once per source, run, test and entry
    catalog.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "FLOAT64"})  # redefined after the test ran

    found = quality.for_entry(catalog, "core.sales.daily_totals")
    assert found["status"] == "fail" and [r["test"] for r in found["failing"]] == ["not_negative"]
    amount = next(r for r in found["results"] if r["test"] == "not_negative")
    assert amount["revision"] == first and amount["current"] is False and amount["failures"] == 4
    assert catalog.get("core.sales.daily_totals.amount")["quality"]["status"] == "fail"

    assert main(["--database-url", database, "import", "--quality", lines(tmp_path, rows), "--source", "great_expectations"]) == 0
    assert "0 results added, 2 already kept" in capsys.readouterr().out
    assert main(["--database-url", database, "quality", "core.sales.daily_totals"]) == 0
    out = capsys.readouterr().out
    assert "fail  core.sales.daily_totals.amount  not_negative  4 failing" in out and "before its latest revision" in out


def test_the_newest_result_as_of_a_time_wins(catalog):
    quality.record(catalog, [{"entry": "core.sales.daily_totals.amount", "test": "t", "status": "fail", "observed_at": "2026-01-01T00:00:00Z", "run": "a"},
                             {"entry": "core.sales.daily_totals.amount", "test": "t", "status": "pass", "observed_at": "2026-02-01T00:00:00Z", "run": "b"}], "dbt")
    assert quality.for_entry(catalog, "core.sales.daily_totals.amount")["status"] == "pass"
    assert quality.for_entry(catalog, "core.sales.daily_totals.amount", at="2026-01-15")["status"] == "fail"


def test_a_bad_line_is_refused_with_its_number_and_nothing_is_kept(catalog):
    with pytest.raises(CatalogError, match="result 2: status"):
        quality.record(catalog, [{"entry": "core.sales.daily_totals", "test": "a", "status": "pass", "observed_at": "2026-01-01"},
                                 {"entry": "core.sales.daily_totals", "test": "b", "status": "maybe", "observed_at": "2026-01-01"}], "x")
    assert quality.for_entry(catalog, "core.sales.daily_totals")["results"] == []


def test_a_project_using_data_that_fails_a_test_is_in_the_overview(catalog):
    catalog.put("project", {"name": "board", "description": "Board pack"})
    catalog.use("board", ["core.sales.daily_totals"])
    quality.record(catalog, [{"entry": "core.sales.daily_totals.amount", "test": "not_negative", "status": "fail", "observed_at": "2026-01-01T00:00:00Z"}], "dbt")
    assert catalog.overview()["failing_and_used"] == [{"project": "project:board", "key": "table:core.sales.daily_totals",
                                                       "failing": "column:core.sales.daily_totals.amount", "test": "not_negative"}]


def test_dbt_test_results_are_read_from_run_results(database, tmp_path):
    manifest = json.loads((DBT / "manifest.json").read_text())
    model = next(uid for uid, n in manifest["nodes"].items() if n.get("resource_type") == "model" and n["name"] == "account_features")
    node = manifest["nodes"][model]
    manifest["nodes"]["test.churn.not_null_account_features_plan_tier.abc"] = {
        "resource_type": "test", "name": "not_null_account_features_plan_tier", "column_name": "plan_tier", "attached_node": model,
        "test_metadata": {"name": "not_null"}, "depends_on": {"nodes": [model]}, "unique_id": "test.churn.not_null_account_features_plan_tier.abc"}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "run_results.json").write_text(json.dumps({"metadata": {"generated_at": "2026-11-20T06:00:00Z", "invocation_id": "inv-1"}, "results": [
        {"unique_id": "test.churn.not_null_account_features_plan_tier.abc", "status": "fail", "failures": 2, "message": "Got 2 results",
         "timing": [{"name": "execute", "completed_at": "2026-11-20T06:00:05Z"}]},
        {"unique_id": model, "status": "success", "timing": []}]}))
    with Catalog(database, actor="ana") as c:
        c.apply(dbt_entries(DBT))
        result = quality.record(c, quality.dbt_results(tmp_path), "dbt")
        assert result == {"added": 1, "unchanged": 0, "unresolved": []}
        [r] = quality.for_entry(c, f"{node['database']}.{node['schema']}.{node['name']}.plan_tier".lower())["results"]
        assert (r["test"], r["kind"], r["status"], r["failures"], r["run"]) == ("not_null_account_features_plan_tier", "not_null", "fail", 2, "inv-1")
