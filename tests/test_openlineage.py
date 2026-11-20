"""OpenLineage events as a source of runs and lineage: what a scheduler, Spark or dbt reports about the jobs it ran."""
import copy
import json
from pathlib import Path

import pytest

from odcl import Catalog, CatalogError, openlineage

EVENT = json.loads((Path(__file__).parent / "fixtures" / "openlineage" / "complete.json").read_text())
RUN = EVENT["run"]["runId"]


def event(**changes):
    e = copy.deepcopy(EVENT)
    e.update(changes)
    return e


@pytest.fixture
def catalog(database):
    with Catalog(database, actor="ana") as c:
        yield c


def test_a_completed_run_registers_its_datasets_and_their_lineage(catalog):
    result = openlineage.ingest(catalog, EVENT)
    assert result["run"] == RUN and result["state"] == "COMPLETE" and result["change"] == "recorded"

    hours = catalog.get("workforce-demo.features.weekly_hours.hours")
    assert hours["dtype"] == "FLOAT" and hours["description"] == "Hours worked since August"
    assert hours["definition"] == "SUM(worked_minutes_asof) / 60"
    # a filter decides which rows count, not the value: it is not where the value comes from
    assert [d["key"] for d in hours["derived_from"]] == ["column:workforce-demo.raw.shift_totals.worked_minutes_asof"]
    up = catalog.lineage("workforce-demo.features.weekly_hours", "up")["upstream"]
    assert [n["key"] for n in up] == ["table:workforce-demo.raw.shift_totals"]
    assert catalog.get("workforce-demo.raw.shift_totals.employee_id")["description"] == "Employee identifier"
    assert hours["source"] == {"openlineage": {"namespace": "bigquery", "name": "workforce-demo.features.weekly_hours"}}

    [run] = catalog.runs(system="openlineage")
    assert run["job"] == "airflow-prod/staffing_daily.build_weekly_hours" and run["state"] == "COMPLETE"
    assert run["inputs"] == ["table:workforce-demo.raw.shift_totals"] and run["outputs"] == ["table:workforce-demo.features.weekly_hours"]
    assert run["parent"] == "0190f1a2-0000-7000-8000-000000000001"


def test_bigquery_datasets_share_keys_with_the_bigquery_importer_and_keep_what_it_said(catalog):
    catalog.put("table", {"path": "workforce-demo.raw.shift_totals", "description": "Shift totals as loaded", "system": "bigquery",
                          "source": {"bigquery": {"type": "TABLE"}}})
    openlineage.ingest(catalog, EVENT)
    table = catalog.get("table:workforce-demo.raw.shift_totals")
    assert table["description"] == "Shift totals as loaded" and set(table["source"]) == {"bigquery", "openlineage"}


def test_a_run_reported_in_pieces_and_out_of_order_is_one_run_and_a_replay_changes_nothing(catalog):
    complete = EVENT
    start = event(eventType="START", eventTime="2026-09-03T04:08:00Z", outputs=[])
    openlineage.ingest(catalog, complete)
    assert openlineage.ingest(catalog, start)["state"] == "COMPLETE"  # a finished run does not go back to running
    [run] = catalog.runs(system="openlineage")
    assert run["started"] == "2026-09-03T04:08:00+00:00" and run["ended"] == "2026-09-03T04:10:00+00:00"
    before = catalog.get("workforce-demo.features.weekly_hours.hours")["revision"]
    assert openlineage.ingest(catalog, complete)["change"] == "duplicate"
    assert catalog.get("workforce-demo.features.weekly_hours.hours")["revision"] == before


def test_a_failed_run_is_recorded_but_says_nothing_about_lineage(catalog):
    failed = event(eventType="FAIL")
    failed["run"] = {"runId": "0190f1a2-9999-7000-8000-000000000009", "facets": {
        "errorMessage": {"message": "Access Denied: Table workforce-demo:raw.shift_totals: User does not have permission", "programmingLanguage": "SQL"}}}
    openlineage.ingest(catalog, failed)
    [run] = catalog.runs(system="openlineage")
    assert run["state"] == "FAIL" and run["outputs"] == ["table:workforce-demo.features.weekly_hours"]
    with pytest.raises(CatalogError):
        catalog.get("workforce-demo.features.weekly_hours.hours")  # what a failed run would have built is not lineage
    stored = json.dumps(catalog.db.execute("SELECT body FROM lineage_events").fetchone()["body"])
    assert "Access Denied" not in stored  # error messages are not kept, as with BigQuery jobs


def test_sql_is_kept_only_as_a_shape(catalog):
    pytest.importorskip("sqlglot")
    openlineage.ingest(catalog, EVENT)
    [run] = catalog.runs(system="openlineage")
    assert "2026-08-01" not in json.dumps(run) and "?" in run["sql"]
    assert "2026-08-01" not in json.dumps(catalog.db.execute("SELECT body FROM lineage_events").fetchone()["body"])


def test_datasets_outside_a_warehouse_get_a_key_and_keep_their_names(catalog):
    files = event()
    files["inputs"] = [{"namespace": "s3://Landing-Bucket", "name": "/exports/2026/Shifts.parquet"}]
    files["outputs"] = [{"namespace": "postgres://db.internal:5432", "name": "staffing.public.weekly"}]
    openlineage.ingest(catalog, files)
    landing = catalog.get("table:s3.landing-bucket.exports.2026.shifts.parquet")
    assert landing["source"]["openlineage"] == {"namespace": "s3://Landing-Bucket", "name": "/exports/2026/Shifts.parquet"}
    weekly = catalog.lineage("staffing.public.weekly", "up")
    assert [n["key"] for n in weekly["upstream"]] == ["table:s3.landing-bucket.exports.2026.shifts.parquet"]


def test_a_job_event_without_a_run_declares_lineage(catalog):
    declared = {k: v for k, v in EVENT.items() if k not in ("run", "eventType")}
    declared["schemaURL"] = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/definitions/JobEvent"
    result = openlineage.ingest(catalog, declared)
    assert result["run"] is None and catalog.runs(system="openlineage") == []
    assert catalog.get("workforce-demo.features.weekly_hours.hours")["definition"] == "SUM(worked_minutes_asof) / 60"


@pytest.mark.parametrize("broken, code", [
    ({"eventType": "FINISHED"}, "openlineage"),
    ({"eventTime": "yesterday"}, "openlineage"),
    ({"job": {"namespace": "airflow-prod"}}, "openlineage"),
    ({"run": {"runId": ""}}, "openlineage"),
])
def test_an_event_that_does_not_follow_the_spec_is_refused_and_writes_nothing(catalog, broken, code):
    with pytest.raises(CatalogError) as error:
        openlineage.ingest(catalog, event(**broken))
    assert error.value.code == code
    assert catalog.find() == [] and catalog.runs() == []


def test_the_command_line_reads_a_file_of_events(database, capsys, tmp_path):
    from odcl.cli import main
    path = tmp_path / "events.jsonl"
    start = event(eventType="START", eventTime="2026-09-03T04:08:00Z")
    path.write_text(json.dumps(start) + "\n" + json.dumps(EVENT) + "\n")
    assert main(["--database-url", database, "import", "--openlineage", str(path)]) == 0
    out = capsys.readouterr().out
    assert "2 events: 2 recorded, 0 duplicate" in out and "airflow-prod/staffing_daily.build_weekly_hours  COMPLETE" in out
