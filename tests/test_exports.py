"""A file set bound to the BigQuery EXPORT DATA job that wrote it: each file field traced through the job's SELECT."""
import json
from datetime import datetime

import pytest

from odcl import Catalog, CatalogError, builds, filesets, vertex
from test_vertex import FILE_JOB, FILE_SET, LOADER_OUT, MODEL, FakeVertex, exported, file_manifest

pytest.importorskip("sqlglot")

SOURCE = "workforce-demo.features.shift_inputs"
EXPORT_SQL = f"""EXPORT DATA OPTIONS (uri = '{LOADER_OUT}/data_*.parquet', format = 'PARQUET', overwrite = true) AS
SELECT employee_id, worked_hours AS hours_at_cutoff, tenure_days
FROM `{SOURCE}` WHERE time_split = 'train' AND valid_for_training = TRUE"""
REF = "workforce-demo:US.job_export_7001"


def ms(at: str) -> str:
    return str(int(datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp() * 1000))


def export_job(job_id="job_export_7001", sql=EXPORT_SQL, kind="EXPORT_DATA", state="DONE", error=None, created="2026-10-05T09:05:00Z",
               files="3", rows="6", project="workforce-demo"):
    return {"jobReference": {"projectId": project, "location": "US", "jobId": job_id},
            "configuration": {"query": {"query": sql}},
            "status": {"state": state, **({"errorResult": {"reason": error}} if error else {})},
            "statistics": {"creationTime": ms(created), "endTime": ms("2026-10-05T09:06:00Z"),
                           "query": {"statementType": kind, "exportDataStatistics": {"fileCount": files, "rowCount": rows}}}}


def client(*jobs, objects=None, created="2026-10-05T09:11:00Z"):
    return FakeVertex(job=FILE_JOB, objects=(objects or exported()) | {"gs://demo-models/staffing/v20/model.bin": (MODEL, "1788318000000000")},
                      bq_jobs=jobs or (export_job(),), created=created)


@pytest.fixture
def catalog(database):
    with Catalog(database, actor="ana") as c:
        c.put("table", {"path": SOURCE, "description": "Shift features", "system": "bigquery"})
        for column in ("employee_id", "worked_hours", "tenure_days", "time_split", "valid_for_training"):
            c.put("column", {"table": SOURCE, "name": column, "description": column.replace("_", " ")})
        yield c


def record(catalog, c=None, **options):
    return filesets.record(catalog, client=c or client(), pipeline_run=FILE_JOB["name"], task="load-data", output="output_train_dataset", **options)


def test_a_renamed_column_is_traced_to_its_source_through_the_export_job(catalog):
    assert record(catalog, export_jobs=[REF])["exported_by"] == [REF]
    trace = builds.explain_dataset(catalog, FILE_SET)
    fields = {c["field"]: c for c in trace["columns"]}
    hours = fields["hours_at_cutoff"]
    assert hours["expression"] == "worked_hours" and hours["lineage"] == "resolved"
    assert [d["column"] for d in hours["derived_from"]] == [f"{SOURCE}.worked_hours"]  # the label is renamed: matching by name would miss it
    assert fields["is_train"]["lineage"] == "not_claimed" and "derived_from" not in fields["is_train"]  # the SELECT does not output it
    assert trace["exported_by"] == [{"job": REF, "statement_type": "EXPORT_DATA", "rows": 6, "files": 3, "created": "2026-10-05T09:05:00+00:00",
                                     "ended": "2026-10-05T09:06:00+00:00"}]
    assert trace["row_filter"] == {"expression": "time_split = ? AND valid_for_training = TRUE",
                                   "columns": [f"{SOURCE}.time_split", f"{SOURCE}.valid_for_training"]}
    # the catalog has no revision from before the job ran, so the definition is pinned as it is now, and the trace says so
    assert any("may differ" in m and SOURCE in m for m in trace["missing"])
    assert not any("column-level lineage below the file set is not claimed" in m for m in trace["missing"])
    assert any("is_train" in m and "not output" in m for m in trace["missing"])


def test_a_definition_recorded_before_the_job_ran_is_the_one_pinned(catalog):
    later = export_job(created="2099-01-01T00:00:00Z")
    record(catalog, client(later, created="2099-01-01T01:00:00Z"), export_jobs=[REF])
    [hours] = [c for c in builds.explain_dataset(catalog, FILE_SET)["columns"] if c["field"] == "hours_at_cutoff"]
    assert hours["derived_from"][0]["pinned"] == "before_export"
    assert not any("may differ" in m for m in builds.explain_dataset(catalog, FILE_SET)["missing"])


def test_a_model_trained_on_bound_files_traces_down_the_catalogs_lineage_and_uses_the_columns(catalog):
    record(catalog, export_jobs=[REF], from_table=SOURCE)
    vertex.import_build(catalog, file_manifest(), client=client())
    trace = catalog.explain_model("staffing-forecast", "v20")
    [feature] = trace["inputs"]
    assert feature["expression"] == "worked_hours" and feature["derived_from"] == [f"{SOURCE}.worked_hours"] and feature["lineage"] == "resolved"
    assert trace["read"]["exported_by"][0]["job"] == REF and trace["read"]["row_filter"]["expression"].startswith("time_split")
    uses = {u["key"]: u["purpose"] for u in catalog.uses("staffing-forecast")["uses"]}
    assert uses == {f"column:{SOURCE}.worked_hours": "feature in v20", f"table:{SOURCE}": "read as files in v20"}


@pytest.mark.parametrize("job, code", [
    (export_job(sql=EXPORT_SQL.replace(LOADER_OUT, "gs://demo-pipelines/elsewhere")), "export_job_elsewhere"),
    (export_job(state="DONE", error="invalidQuery"), "export_job_failed"),
    (export_job(state="RUNNING"), "export_job_failed"),
    (export_job(kind="SELECT"), "export_job"),
    (export_job(created="2026-10-05T09:30:00Z"), "export_after_files"),
    (export_job(rows="7"), "export_rows_differ"),
    (export_job(files="40", rows="7"), "export_rows_differ"),
])
def test_a_job_that_cannot_have_written_these_files_is_refused_and_nothing_is_written(catalog, job, code):
    with pytest.raises(CatalogError) as error:
        record(catalog, client(job), export_jobs=[REF])
    assert error.value.code == code
    assert catalog.db.execute("SELECT count(*) AS n FROM dataset_versions").fetchone()["n"] == 0


def test_files_rewritten_after_the_export_are_bound_when_their_row_count_is_the_jobs(catalog):
    record(catalog, client(export_job(files="4127", rows="6")), export_jobs=[REF])  # compacted afterwards: 3 files now, the same 6 rows
    assert any("rewritten after the export" in m and "row count equal" in m for m in builds.explain_dataset(catalog, FILE_SET)["missing"])


def test_recording_again_changes_nothing_and_an_unbound_file_set_can_be_bound_once(catalog):
    record(catalog)
    assert record(catalog, export_jobs=[REF])["change"] == "bound"
    assert record(catalog, export_jobs=[REF])["change"] == "unchanged"
    other = export_job(job_id="job_export_other")
    with pytest.raises(CatalogError) as error:
        record(catalog, client(other), export_jobs=["workforce-demo:US.job_export_other"])
    assert error.value.code == "conflict"


def test_a_source_table_the_catalog_does_not_have_is_named(catalog):
    sql = EXPORT_SQL.replace(SOURCE, "workforce-demo.features.unknown_table")
    with pytest.raises(CatalogError) as error:
        record(catalog, client(export_job(sql=sql)), export_jobs=[REF])
    assert error.value.code == "missing_dependency" and "unknown_table" in str(error.value)


def test_the_export_job_is_found_in_the_tasks_run(catalog):
    unrelated = export_job(job_id="job_other_export", sql=EXPORT_SQL.replace(LOADER_OUT, "gs://demo-pipelines/other"))
    query = export_job(job_id="job_select", kind="SELECT", sql="SELECT 1")
    assert record(catalog, client(export_job(), unrelated, query), find_export_job=True)["exported_by"] == [REF]


def test_finding_no_export_job_or_several_is_refused_and_names_them(catalog):
    with pytest.raises(CatalogError) as error:
        record(catalog, client(export_job(created="2026-10-04T00:00:00Z")), find_export_job=True)  # outside the task's run
    assert error.value.code == "export_job_not_found"
    second = export_job(job_id="job_export_7002")
    with pytest.raises(CatalogError) as error:
        record(catalog, client(export_job(), second), find_export_job=True)
    assert error.value.code == "export_job_ambiguous" and "job_export_7001" in str(error.value) and "job_export_7002" in str(error.value)


def test_the_command_line_binds_and_explains(database, monkeypatch, capsys):
    from odcl.cli import main
    with Catalog(database) as c:
        c.put("table", {"path": SOURCE, "description": "Shift features", "system": "bigquery"})
        for column in ("employee_id", "worked_hours", "tenure_days", "time_split", "valid_for_training"):
            c.put("column", {"table": SOURCE, "name": column})
    monkeypatch.setattr(vertex, "client", lambda location: client())
    db = ["--database-url", database]
    assert main([*db, "record", "dataset-files", "--pipeline-run", FILE_JOB["name"], "--task", "load-data", "--output", "output_train_dataset",
                 "--export-job", REF]) == 0
    assert f"exported by {REF}" in capsys.readouterr().out
    assert main([*db, "explain", "dataset", FILE_SET]) == 0
    out = capsys.readouterr().out
    assert f"exported by   BigQuery job ({REF})  EXPORT_DATA  6 rows" in out and "row filter    time_split = ? AND valid_for_training = TRUE" in out
    assert f"hours_at_cutoff = worked_hours  from {SOURCE}.worked_hours" in out
    assert json.loads(json.dumps(builds.explain_dataset(Catalog(database), FILE_SET)))  # serialisable


SIDECAR = f"{LOADER_OUT}/metadata.json"


def with_sidecar():
    return exported() | {SIDECAR: (b'{"rows": 6, "written_by": "load-data"}', "1791000000000050")}


@pytest.mark.parametrize("files", ["3", "4127"])  # the job's own files, or compacted afterwards: either way only the Parquet is counted
def test_a_non_parquet_sidecar_is_not_counted_against_the_export_job(catalog, files):
    record(catalog, client(export_job(files=files, rows="6"), objects=with_sidecar()), export_jobs=[REF])
    trace = builds.explain_dataset(catalog, FILE_SET)
    assert len(trace["objects"]) == 4  # the sidecar is still recorded and pinned
    assert any("metadata.json" in m and "not Parquet" in m and "not counted" in m for m in trace["missing"])
    assert any("rewritten after the export" in m for m in trace["missing"]) == (files != "3")


def test_parquet_rows_that_differ_from_the_jobs_are_refused_even_beside_a_sidecar(catalog):
    with pytest.raises(CatalogError) as error:
        record(catalog, client(export_job(files="4127", rows="7"), objects=with_sidecar()), export_jobs=[REF])
    assert error.value.code == "export_rows_differ" and "7" in str(error.value) and "6" in str(error.value)


def test_a_file_set_recorded_before_row_counts_were_kept_binds_when_recorded_again(catalog):
    record(catalog, client(objects=with_sidecar()))
    # as a release before migration 0007 left it: no row counts or creation times on the objects
    catalog.db.execute("UPDATE dataset_version_objects SET num_rows = NULL, time_created = NULL")
    assert record(catalog, client(export_job(files="4127", rows="6"), objects=with_sidecar()), export_jobs=[REF])["change"] == "bound"
    rows = {r["uri"].rsplit("/", 1)[1]: r["num_rows"] for r in catalog.db.execute("SELECT uri, num_rows FROM dataset_version_objects")}
    assert rows == {"part-0.parquet": 2, "part-1.parquet": 2, "part-2.parquet": 2, "metadata.json": None}  # filled in from the footers
