"""BigQuery schemas and query-job history, read through Sounding and kept in the one Postgres database."""
import datetime as dt
import json
import logging
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

pytest.importorskip("google.cloud.bigquery", reason="the BigQuery importer needs the bigquery extra")
from google.cloud import bigquery as gbq

from odcl import Catalog, CatalogError
from odcl import bigquery as bq
from odcl import store
from odcl.cli import main

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 9, 2, 1, 58, 40, tzinfo=UTC)  # when the fixture's jobs were created
UNTIL = dt.datetime(2026, 9, 2, 6, tzinfo=UTC)
FEATURES = "data-p.features.shift_inputs"
BY_EMPLOYEE = "SELECT hours_at_cutoff FROM `data-p.features.shift_inputs` WHERE employee_id = '{}' -- asked by pat@example.com"


def ref(fqn):
    return dict(zip(("projectId", "datasetId", "tableId"), fqn.split("."), strict=True))


def job(name, *, statement="SELECT", reads=(), writes=None, billed=0, sql="SELECT 1", state="DONE", error=None, parent=None, children=0, labels=None, **query):
    stats = {"creationTime": str(int(T0.timestamp() * 1000)), "startTime": str(int(T0.timestamp() * 1000) + 1000),
             "query": {"statementType": statement, "totalBytesBilled": str(billed), "totalBytesProcessed": str(billed), "totalSlotMs": "50",
                       "referencedTables": [ref(t) for t in reads]} | query}
    if state == "DONE":
        stats["endTime"] = str(int(T0.timestamp() * 1000) + 72000)
    if parent:
        stats["parentJobId"] = parent
    if children:
        stats["numChildJobs"] = str(children)
    config = {"query": {"query": sql} | ({"destinationTable": ref(writes)} if writes else {})} | ({"labels": labels} if labels else {})
    status = {"state": state} | ({"errorResult": {"reason": error, "message": "no employee E-1001"}} if error else {})
    return gbq.QueryJob.from_api_repr({"jobReference": {"projectId": "jobs-p", "jobId": name, "location": "us-central1"},
                                       "configuration": config, "statistics": stats, "status": status}, Mock())


def week():
    """One window of a project's jobs: a build that reads another project, a script, a cache hit, a failure and two lookups."""
    return [
        [job("ctas", statement="CREATE_TABLE_AS_SELECT", reads=["other-p.raw.shift_totals"], writes=FEATURES, billed=100,
             sql="CREATE OR REPLACE TABLE `data-p.features.shift_inputs` AS SELECT worked_minutes_asof / 60 AS hours_at_cutoff FROM `other-p.raw.shift_totals`"),
         job("script-1", statement="SCRIPT", billed=300, children=1, sql="BEGIN INSERT INTO t SELECT 1; END"),
         job("cached", cacheHit=True, writes="jobs-p._a1b2c3.anon9f")],
        [job("failed", error="invalidQuery", sql="SELECT FROM WHERE ("),
         job("lookup-a", reads=[FEATURES], billed=10, sql=BY_EMPLOYEE.format("E-1001")),
         job("lookup-b", reads=[FEATURES], billed=10, sql=BY_EMPLOYEE.format("E-2002")),
         job("monitor", reads=[FEATURES], billed=7, labels={"sounding_action": "query_opportunities"})],
    ]


CHILDREN = {"script-1": [job("script-1_0", statement="INSERT", reads=[FEATURES], writes="data-p.features.audit", billed=300, parent="script-1",
                             sql="INSERT INTO `data-p.features.audit` SELECT * FROM `data-p.features.shift_inputs`")]}


def client_for(pages, children=CHILDREN, fail_after=None):
    """A BigQuery client that lists these pages. With `fail_after`, the connection drops after that many pages."""
    class Page(list):
        _raw_page = {}

    def listed():
        for n, page in enumerate(pages):
            if n == fail_after:
                raise ConnectionError("connection reset")
            yield Page(page)

    def list_jobs(**kwargs):
        return NS(pages=iter([Page(children.get(kwargs["parent_job"], []))])) if kwargs.get("parent_job") else NS(pages=listed())
    return Mock(project="jobs-p", list_jobs=Mock(side_effect=list_jobs))


@pytest.fixture
def catalog(database):
    with Catalog(database, actor="ana") as c:
        yield c


def test_a_window_of_jobs_is_kept_once_however_often_it_is_read(catalog):
    first = bq.sync_jobs(catalog, "jobs-p", client=client_for(week()), until=UNTIL)
    assert (first["jobs"], first["added"], first["excluded"]) == (7, 7, 1)  # the monitoring tool's own job is left out
    assert first["since"] == "2026-08-26T06:00:00+00:00" and first["until"] == "2026-09-02T06:00:00+00:00"  # a bounded first backfill

    again = bq.sync_jobs(catalog, "jobs-p", client=client_for(week()), until=UNTIL + dt.timedelta(hours=1))
    assert (again["jobs"], again["added"]) == (7, 0)
    assert again["since"] == "2026-09-02T00:00:00+00:00"  # six hours before the last window ended: no job runs longer

    history = catalog.table_history(FEATURES, since=T0 - dt.timedelta(days=1))
    by_id = {j["job_id"]: j for j in history["jobs"]}
    assert sorted(by_id) == ["ctas", "lookup-a", "lookup-b", "script-1_0"]
    assert by_id["ctas"]["roles"] == ["write"] and by_id["ctas"]["statement_type"] == "CREATE_TABLE_AS_SELECT"
    assert by_id["script-1_0"]["roles"] == ["read"] and by_id["script-1_0"]["parent_job_id"] == "script-1"
    assert by_id["ctas"]["project"] == "jobs-p" and by_id["ctas"]["location"] == "us-central1"  # where it ran, not where the table lives
    assert history["totals"] == {"jobs": 4, "billed_bytes": 420, "slot_ms": 200}
    assert history["synced"] == [{"project": "jobs-p", "until": "2026-09-02T07:00:00+00:00"}]

    # a read of another project's table is found from that table, and a script's write from its child statement
    assert [j["job_id"] for j in catalog.table_history("other-p.raw.shift_totals", since=T0 - dt.timedelta(days=1))["jobs"]] == ["ctas"]
    audit = catalog.table_history("table:data-p.features.audit", since=T0 - dt.timedelta(days=1))["jobs"]
    assert [(j["job_id"], j["roles"]) for j in audit] == [("script-1_0", ["write"])]


def test_jobs_that_tell_little_are_kept_with_what_is_missing(catalog):
    bq.sync_jobs(catalog, "jobs-p", client=client_for(week()), until=UNTIL)
    jobs = {j["job_id"]: j for j in catalog.query_jobs("jobs-p")}
    assert jobs["cached"]["cache_hit"] and jobs["cached"]["gaps"] == ["no_table_references"]
    assert jobs["cached"]["writes"] == []  # a result set in BigQuery's hidden dataset is not a lasting write
    assert jobs["failed"]["error_reason"] == "invalidQuery" and set(jobs["failed"]["gaps"]) == {"no_table_references", "template_unavailable"}
    assert jobs["ctas"]["writes"] == [FEATURES] and jobs["ctas"]["reads"] == ["other-p.raw.shift_totals"] and jobs["ctas"]["gaps"] == []


def test_a_table_read_as_of_a_time_or_by_partition_is_still_that_table(catalog):
    reads = [FEATURES + "@1788314400000", FEATURES + "$20260901", FEATURES]
    bq.sync_jobs(catalog, "jobs-p", client=client_for([[job("as-of", reads=reads, writes="data-p.features.daily$20260901")]]), until=UNTIL)
    assert [(j["job_id"], j["reads"], j["writes"]) for j in catalog.query_jobs("jobs-p")] == [("as-of", [FEATURES], ["data-p.features.daily"])]
    assert catalog.table_history(FEATURES, since=T0 - dt.timedelta(days=1))["totals"]["jobs"] == 1


def test_an_interrupted_read_is_not_recorded_as_done_and_a_retry_completes_it(catalog):
    with pytest.raises(ConnectionError):
        bq.sync_jobs(catalog, "jobs-p", client=client_for(week(), fail_after=1), until=UNTIL)
    partial = catalog.table_history(FEATURES, since=T0 - dt.timedelta(days=1))
    assert [j["job_id"] for j in partial["jobs"]] == ["ctas", "script-1_0"] and partial["synced"] == []  # the first page is kept; the window is not

    retry = bq.sync_jobs(catalog, "jobs-p", client=client_for(week()), until=UNTIL)
    assert (retry["jobs"], retry["added"], retry["since"]) == (7, 3, "2026-08-26T06:00:00+00:00")
    assert catalog.table_history(FEATURES, since=T0 - dt.timedelta(days=1))["totals"]["jobs"] == 4


def test_a_running_job_is_read_again_until_it_ends_and_never_goes_back(catalog):
    running = job("slow", state="RUNNING", reads=[FEATURES])
    del running._properties["statistics"]["query"]["totalBytesBilled"]
    bq.sync_jobs(catalog, "jobs-p", client=client_for([[running]]), until=T0 + dt.timedelta(minutes=1))
    assert [(j["state"], j["billed_bytes"]) for j in catalog.query_jobs("jobs-p")] == [("RUNNING", None)]
    bq.sync_jobs(catalog, "jobs-p", client=client_for([[running]]), until=T0 + dt.timedelta(days=2))  # still running two days on

    later = client_for([[job("slow", reads=[FEATURES], billed=55)]])
    bq.sync_jobs(catalog, "jobs-p", client=later, until=T0 + dt.timedelta(days=3))
    assert later.list_jobs.call_args_list[0].kwargs["min_creation_time"] <= T0  # days on, the window still reaches the unfinished job
    assert [(j["state"], j["billed_bytes"]) for j in catalog.query_jobs("jobs-p")] == [("DONE", 55)]

    bq.sync_jobs(catalog, "jobs-p", client=client_for([[running]]), until=T0 + dt.timedelta(days=3, hours=1))  # a stale answer
    assert [(j["state"], j["billed_bytes"]) for j in catalog.query_jobs("jobs-p")] == [("DONE", 55)]


def test_queries_that_differ_only_in_their_literals_are_one_example_without_the_literals(catalog, caplog):
    caplog.set_level(logging.DEBUG)
    bq.sync_jobs(catalog, "jobs-p", client=client_for(week()), until=UNTIL)
    history = catalog.table_history(FEATURES, since=T0 - dt.timedelta(days=1), patterns=True)
    lookups = [p for p in history["patterns"] if p["executions"] == 2]
    assert len(lookups) == 1 and len(history["patterns"]) == 3
    assert lookups[0]["template"] == "SELECT hours_at_cutoff FROM `data-p.features.shift_inputs` WHERE employee_id = ?"
    assert lookups[0]["example_job"]["job_id"] in ("lookup-a", "lookup-b") and lookups[0]["last_run"] == "2026-09-02T01:58:40+00:00"
    everything = json.dumps([history, catalog.query_jobs("jobs-p")]) + caplog.text
    assert "E-1001" not in everything and "E-2002" not in everything and "pat@example.com" not in everything


def test_without_sql_capture_no_sql_is_asked_for_or_kept(catalog):
    client = client_for(week())
    asked = []
    real = bq.source.job_pages
    bq_pages = lambda *a, **k: asked.append(k["include_sql"]) or real(*a, **k)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(bq.source, "job_pages", bq_pages)
        bq.sync_jobs(catalog, "jobs-p", client=client, until=UNTIL, sql="none")
    assert asked == [False]
    assert catalog.table_history(FEATURES, since=T0 - dt.timedelta(days=1), patterns=True)["patterns"] == []
    with pytest.raises(CatalogError):
        bq.sync_jobs(catalog, "jobs-p", client=client, until=UNTIL, sql="full")  # original text is not offered yet


def test_tables_are_registered_with_nested_columns_and_importing_again_changes_nothing(catalog):
    snapshot = gbq.Table("data-p.frozen.shift_inputs_b042", schema=[
        gbq.SchemaField("employee_id", "STRING", mode="REQUIRED"),
        gbq.SchemaField("Shift", "RECORD", fields=[gbq.SchemaField("hours_at_cutoff", "FLOAT", description="Hours at the cutoff")])])
    snapshot._properties |= {"type": "SNAPSHOT", "description": "Frozen inputs for build 042", "numRows": "10", "snapshotDefinition": {
        "baseTableReference": ref(FEATURES), "snapshotTime": "2026-09-02T02:00:00Z"}}
    client = Mock(get_table=Mock(return_value=snapshot))

    assert bq.import_tables(catalog, ["data-p.frozen.shift_inputs_b042"], client=client) == {"added": 4, "changed": 0, "unchanged": 0}
    table = catalog.get("data-p.frozen.shift_inputs_b042")
    assert (table["system"], table["type"], table["description"]) == ("bigquery", "snapshot", "Frozen inputs for build 042")
    assert table["source"]["bigquery"]["base_table"] == FEATURES and table["source"]["bigquery"]["snapshot_time"] == "2026-09-02T02:00:00Z"
    assert [(c["key"].rsplit("shift_inputs_b042.", 1)[1], c["dtype"]) for c in table["columns"]] == [
        ("employee_id", "STRING"), ("shift", "RECORD"), ("shift.hours_at_cutoff", "FLOAT")]
    nested = catalog.get("column:data-p.frozen.shift_inputs_b042.shift.hours_at_cutoff")
    assert nested["description"] == "Hours at the cutoff" and nested["source"]["bigquery"]["path"] == "Shift.hours_at_cutoff"  # BigQuery's own spelling

    snapshot._properties["numRows"] = "999"  # row counts move without the definition changing
    assert bq.import_tables(catalog, ["data-p.frozen.shift_inputs_b042"], client=client) == {"added": 0, "changed": 0, "unchanged": 4}


def test_the_command_line_syncs_and_shows_a_tables_history(database, monkeypatch, capsys):
    client = client_for(week())
    monkeypatch.setattr(bq, "client", lambda project: client)
    monkeypatch.setattr(store, "now", lambda: UNTIL)
    db = ["--database-url", database]
    assert main([*db, "sync", "bigquery-jobs", "--project", "jobs-p", "--backfill", "7d"]) == 0
    assert "7 jobs read, 7 new" in capsys.readouterr().out
    assert main([*db, "history", "table", FEATURES, "--since", "7d", "--patterns"]) == 0
    out = capsys.readouterr().out
    assert "4 job(s)" in out and "employee_id = ?" in out and "E-1001" not in out
    assert main([*db, "--json", "history", "table", FEATURES, "--since", "2h"]) == 0
    assert json.loads(capsys.readouterr().out)["totals"]["jobs"] == 0  # the jobs ran four hours before "now"
    assert main([*db, "history", "table", FEATURES, "--since", "soon"]) == 1 and "7d" in capsys.readouterr().err


def test_an_explicit_window_is_read_once_and_leaves_the_regular_sync_where_it_was(catalog):
    client = client_for(week())
    since = T0 - dt.timedelta(minutes=5)
    result = bq.sync_jobs(catalog, "jobs-p", client=client, since=since, until=T0 + dt.timedelta(minutes=31))
    assert result["window"] == "explicit" and result["jobs"] == 7 and result["since"] == "2026-09-02T01:53:40+00:00"
    asked = client.list_jobs.call_args_list[0].kwargs
    assert (asked["min_creation_time"], asked["max_creation_time"]) == (since, T0 + dt.timedelta(minutes=31))
    assert catalog.db.execute("SELECT count(*) AS n FROM sync_cursors").fetchone()["n"] == 0  # the time before it is still unread
    regular = bq.sync_jobs(catalog, "jobs-p", client=client_for(week()), until=UNTIL)
    assert regular["window"] == "incremental" and regular["since"] == "2026-08-26T06:00:00+00:00" and regular["added"] == 0
    with pytest.raises(CatalogError, match="since is not before until"):
        bq.sync_jobs(catalog, "jobs-p", client=client_for(week()), since=UNTIL, until=T0)


def test_the_command_line_reads_the_jobs_of_a_pipeline_task_or_a_window(database, monkeypatch, capsys):
    from pathlib import Path

    from odcl import vertex
    run = json.loads((Path(__file__).parent / "fixtures" / "file_trace" / "vertex_pipeline_job.json").read_text())
    clients = []
    monkeypatch.setattr(bq, "client", lambda project: clients.append(client_for(week())) or clients[-1])
    monkeypatch.setattr(vertex, "client", lambda location: Mock(get=Mock(return_value=run)))
    db = ["--database-url", database, "sync", "bigquery-jobs", "--project", "jobs-p"]
    assert main([*db, "--pipeline-run", run["name"], "--task", "load-data"]) == 0
    assert "this window only" in capsys.readouterr().out
    asked = clients[-1].list_jobs.call_args_list[0].kwargs
    assert (asked["min_creation_time"].isoformat(), asked["max_creation_time"].isoformat()) == ("2026-10-05T09:01:00+00:00", "2026-10-05T09:12:00+00:00")
    assert main([*db, "--since", "2026-10-05T09:00:00", "--until", "2026-10-05T09:36:00Z"]) == 0
    asked = clients[-1].list_jobs.call_args_list[0].kwargs
    assert (asked["min_creation_time"].isoformat(), asked["max_creation_time"].isoformat()) == ("2026-10-05T09:00:00+00:00", "2026-10-05T09:36:00+00:00")
    assert main([*db, "--pipeline-run", run["name"], "--since", "2026-10-05"]) == 1 and "not both" in capsys.readouterr().err
    assert main([*db, "--since", "yesterday"]) == 1 and "is not a time" in capsys.readouterr().err


def test_a_foreign_key_constraint_becomes_a_reference_and_one_to_a_table_not_read_is_named(catalog):
    def table(fqn, fields, keys=()):
        found = gbq.Table(fqn, schema=[gbq.SchemaField(f, "STRING") for f in fields])
        found._properties["tableConstraints"] = {"foreignKeys": [
            {"name": f"fk_{n}", "referencedTable": ref(target), "columnReferences": [{"referencingColumn": a, "referencedColumn": b}]}
            for n, (a, target, b) in enumerate(keys)]}
        return found
    tables = {"p.s.orders": table("p.s.orders", ["id", "Customer_ID", "store_id"], [("Customer_ID", "p.s.customers", "ID"), ("store_id", "p.s.stores", "id")]),
              "p.s.customers": table("p.s.customers", ["ID"])}
    client = Mock(get_table=Mock(side_effect=lambda r: tables[f"{r.project}.{r.dataset_id}.{r.table_id}"]))
    result = bq.import_tables(catalog, ["p.s.orders", "p.s.customers"], client=client)  # the referenced table is read after the one that references it
    assert result["unresolved_references"] == ["column:p.s.orders.store_id -> column:p.s.stores.id"]
    assert [r["key"] for r in catalog.get("p.s.orders.customer_id")["references"]] == ["column:p.s.customers.id"]
    again = bq.import_tables(catalog, ["p.s.orders", "p.s.customers"], client=client)
    assert (again["added"], again["changed"]) == (0, 0)


def test_an_end_in_the_future_reads_and_records_only_up_to_now(catalog):
    later = store.now() + dt.timedelta(days=30)
    result = bq.sync_jobs(catalog, "jobs-p", client=client_for(week()), until=later)
    recorded = catalog.db.execute("SELECT completed_until FROM sync_cursors").fetchone()["completed_until"]
    assert recorded <= store.now() and dt.datetime.fromisoformat(result["until"]) <= store.now()
    bq.sync_jobs(catalog, "jobs-p", client=client_for(week()))  # the next regular sync is not refused as already read
