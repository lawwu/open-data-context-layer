"""A model version, from its Vertex AI training task back to the raw column behind each feature.

`expected_explain.json` is the independent statement of the whole trace; the first test checks the answer against
it field for field.
"""
import hashlib
import json
from pathlib import Path

import pytest

from odcl import Catalog, CatalogError, builds, dataform, filesets, manifest, vertex
from test_dataform import INVOCATION, TARGET, FakeDataform, load, snapshot_facts, sync_history

FIXTURES = Path(__file__).parent / "fixtures" / "staffing_trace"
EXPECTED = json.loads((FIXTURES / "expected_explain.json").read_text())
MODEL = (FIXTURES / "model.bin").read_bytes()


class FakeVertex:
    """Answers the pipeline-job read and the two Cloud Storage reads from the fixture."""

    def __init__(self, job=None, objects=None, bq_jobs=(), created="2026-10-05T09:11:00Z"):
        self.pipeline_job = job or load("vertex_pipeline_job.json")
        self.objects = objects or {"gs://demo-models/staffing/v17/model.bin": (MODEL, "1788318000000000")}
        self.bq_jobs, self.created = {j["jobReference"]["jobId"]: j for j in bq_jobs}, created

    def get(self, name):
        assert name == self.pipeline_job["name"], name
        return self.pipeline_job

    def object(self, uri, missing_ok=False):
        if missing_ok and uri not in self.objects:
            return None
        data, generation = self.objects[uri]
        return {"generation": generation, "size": str(len(data))}

    def read(self, uri, generation):
        data, current = self.objects[uri]
        assert generation == current
        return data

    def list(self, prefix):
        """What Cloud Storage's objects.list answers for a prefix: every object whose name starts with it."""
        return [{"name": uri.split("/", 3)[3], "bucket": uri.split("/")[2], "generation": generation, "size": str(len(data)),
                 "crc32c": "AAAAAA==", "md5Hash": "1B2M2Y8AsgTpgAmY7PhCfg==", "timeCreated": self.created}
                for uri, (data, generation) in sorted(self.objects.items()) if uri.startswith(prefix)]

    def job(self, project, location, job_id):
        assert (project, location) == tuple(self.bq_jobs[job_id]["jobReference"][k] for k in ("projectId", "location"))
        return self.bq_jobs[job_id]

    def jobs(self, project, min_ms, max_ms):
        return [j for j in self.bq_jobs.values() if j["jobReference"]["projectId"] == project and min_ms <= int(j["statistics"]["creationTime"]) <= max_ms]

    def tail(self, uri, generation, size):
        data, current = self.objects[uri]
        assert generation == current
        return data[-size:]


@pytest.fixture
def catalog(database):
    with Catalog(database, actor="ana") as c:
        yield c


def build(catalog, invocation=INVOCATION, snapshot=None):
    dataform.import_invocation(catalog, invocation, client=FakeDataform())
    builds.record_handoff(catalog, invocation, TARGET, snapshot or snapshot_facts())


def test_a_model_version_explains_itself_back_to_the_raw_column(catalog):
    sync_history(catalog)
    build(catalog)
    recorded = vertex.import_build(catalog, load("training_manifest.json"), client=FakeVertex())
    assert recorded == {"model": "staffing-forecast", "version": "v17", "change": "added", "inputs": 1, "artifact_verified": "sha256"}
    assert catalog.explain_model("staffing-forecast", "v17") == EXPECTED
    project = catalog.get("project:staffing-forecast")
    assert project["kind"] == "project" and "vertex" in project["source"]  # the model family is a project that uses data


def test_an_identical_replay_changes_nothing_and_a_different_artifact_for_the_same_version_is_refused(catalog):
    build(catalog)
    vertex.import_build(catalog, load("training_manifest.json"), client=FakeVertex())
    assert vertex.import_build(catalog, load("training_manifest.json"), client=FakeVertex())["change"] == "unchanged"

    other = load("training_manifest.json")
    other["artifact"] |= {"uri": "gs://demo-models/staffing/v17b/model.bin", "sha256": hashlib.sha256(b"other").hexdigest()}
    with pytest.raises(CatalogError) as error:
        vertex.import_build(catalog, other, client=FakeVertex(objects={"gs://demo-models/staffing/v17b/model.bin": (b"other", "7")} | FakeVertex().objects))
    assert error.value.code == "conflict" and "v17" in str(error.value)


def test_an_artifact_that_does_not_match_its_manifest_is_refused(catalog):
    build(catalog)
    for objects, code in (({"gs://demo-models/staffing/v17/model.bin": (MODEL, "999")}, "artifact_replaced"),
                          ({"gs://demo-models/staffing/v17/model.bin": (b"tampered", "1788318000000000")}, "artifact_digest")):
        with pytest.raises(CatalogError) as error:
            vertex.import_build(catalog, load("training_manifest.json"), client=FakeVertex(objects=objects))
        assert error.value.code == code


def test_a_missing_dependency_is_named_and_nothing_is_written_until_it_is_there(catalog):
    with pytest.raises(CatalogError) as error:
        vertex.import_build(catalog, load("training_manifest.json"), client=FakeVertex())
    assert error.value.code == "missing_dependency" and EXPECTED["read"]["dataset_version"] in str(error.value)
    assert catalog.db.execute("SELECT count(*) AS n FROM executions WHERE system = 'vertex'").fetchone()["n"] == 0

    build(catalog)
    wrong = load("training_manifest.json")
    wrong["inputs"].append({"field": "tenure_days", "role": "feature", "position": 1})
    with pytest.raises(CatalogError) as error:
        vertex.import_build(catalog, wrong, client=FakeVertex())
    assert error.value.code == "missing_dependency" and "tenure_days" in str(error.value)
    assert vertex.import_build(catalog, load("training_manifest.json"), client=FakeVertex())["change"] == "added"  # the retry starts clean


def test_a_task_that_did_not_succeed_or_is_not_in_the_run_cannot_have_produced_the_model(catalog):
    build(catalog)
    failed = load("vertex_pipeline_job.json")
    failed["jobDetail"]["taskDetails"][0]["state"] = "FAILED"
    with pytest.raises(CatalogError) as error:
        vertex.import_build(catalog, load("training_manifest.json"), client=FakeVertex(job=failed))
    assert error.value.code == "task_not_succeeded"
    elsewhere = load("training_manifest.json") | {"task": "evaluate"}
    with pytest.raises(CatalogError) as error:
        vertex.import_build(catalog, elsewhere, client=FakeVertex())
    assert error.value.code == "task_not_found"
    other_execution = load("training_manifest.json") | {"execution": "projects/workforce-demo/locations/us-central1/metadataStores/default/executions/9"}
    with pytest.raises(CatalogError) as error:
        vertex.import_build(catalog, other_execution, client=FakeVertex())
    assert error.value.code == "execution_mismatch"


SECOND = INVOCATION.replace("build-042", "build-049")


def redefine_and_rebuild(catalog):
    """The feature is redefined, rebuilt and snapshotted again, ready for a new version to be trained on it."""
    compilation = load("dataform_compilation_result.json")
    compilation["compilationResult"] |= {"name": compilation["compilationResult"]["name"] + "-next", "createTime": "2026-09-09T00:00:00Z"}
    compilation["compilationResultActions"][1]["relation"]["selectQuery"] = (
        "SELECT employee_id, pay_period_start, ROUND(worked_minutes_asof / 60, 2) AS hours_at_cutoff FROM `workforce-demo.raw.shift_totals`")
    rebuilt = json.loads(json.dumps(load("dataform_workflow_invocation.json")).replace("build-042", "build-049").replace("job-042", "job-049")
                         .replace("2026-09-02T01:5", "2026-09-09T01:5"))
    rebuilt["workflowInvocation"]["resolvedCompilationResult"] = compilation["compilationResult"]["name"]
    dataform.import_invocation(catalog, SECOND, client=FakeDataform(invocation=rebuilt, compilation=compilation))
    builds.record_handoff(catalog, SECOND, TARGET, snapshot_facts(time="2026-09-09T02:00:00Z") | {"table": "workforce-demo.frozen.shift_inputs_b049"})


def train_v18(catalog):
    v18 = json.loads(json.dumps(load("training_manifest.json")).replace("v17", "v18").replace("train-042", "train-049")
                     .replace("shift_inputs_b042@2026-09-02", "shift_inputs_b049@2026-09-09").replace("executions/481", "executions/489"))
    job = json.loads(json.dumps(load("vertex_pipeline_job.json")).replace("train-042", "train-049").replace("2026-09-02T0", "2026-09-09T0")
                     .replace("executions/481", "executions/489"))
    return vertex.import_build(catalog, v18, client=FakeVertex(job=job, objects={"gs://demo-models/staffing/v18/model.bin": (MODEL, "1788318000000000")}))


def test_a_later_version_trained_on_a_changed_definition_leaves_the_earlier_trace_alone(catalog):
    sync_history(catalog)
    build(catalog)
    vertex.import_build(catalog, load("training_manifest.json"), client=FakeVertex())
    redefine_and_rebuild(catalog)
    train_v18(catalog)

    assert catalog.explain_model("staffing-forecast", "v17") == EXPECTED  # unchanged by everything that came after
    newer = catalog.explain_model("staffing-forecast", "v18")["inputs"][0]
    assert newer["expression"] == "ROUND(worked_minutes_asof / 60, 2)" and newer["produced_by"]["dataform_invocation"] == SECOND
    assert "job job-049 is not in the synced query history of workforce-demo" in catalog.explain_model("staffing-forecast", "v18")["missing"]


FEATURE = f"column:{TARGET}.hours_at_cutoff"
RAW_MINUTES = "workforce-demo.raw.shift_totals.worked_minutes_asof"


def test_a_model_version_records_what_its_project_uses_pinned_as_it_was_trained(catalog):
    build(catalog)
    vertex.import_build(catalog, load("training_manifest.json"), client=FakeVertex())
    [used] = catalog.uses("staffing-forecast")["uses"]
    pinned = catalog.db.execute("SELECT column_revision FROM version_columns WHERE column_key = %s", (FEATURE,)).fetchone()["column_revision"]
    assert used["key"] == FEATURE and used["purpose"] == "feature in v17" and used["linked_revision"] == pinned and not used["changed_since"]
    # so the question "who depends on this raw column?" now reaches the model
    assert {"project": "project:staffing-forecast", "how": "downstream"}.items() <= catalog.used_by(RAW_MINUTES)["projects"][0].items()

    redefine_and_rebuild(catalog)  # the feature changed after v17 was trained on it
    assert catalog.uses("staffing-forecast")["uses"][0]["changed_since"]
    assert {"project": "project:staffing-forecast", "key": FEATURE} in catalog.overview()["changed_since_used"]

    train_v18(catalog)  # retraining on the new definition clears it
    [used] = catalog.uses("staffing-forecast")["uses"]
    assert used["purpose"] == "feature in v18" and not used["changed_since"] and catalog.overview()["changed_since_used"] == []


def test_importing_an_older_version_late_does_not_move_the_project_back(catalog):
    build(catalog)
    redefine_and_rebuild(catalog)
    train_v18(catalog)
    vertex.import_build(catalog, load("training_manifest.json"), client=FakeVertex())  # v17, trained earlier, imported after
    assert catalog.uses("staffing-forecast")["uses"][0]["purpose"] == "feature in v18"


def test_what_a_person_recorded_a_project_using_is_kept(catalog):
    build(catalog)
    catalog.put("project", {"name": "staffing-forecast", "description": "Weekly staffing forecast"})
    catalog.use("staffing-forecast", ["workforce-demo.raw.shift_totals"], "EDA of shift totals")
    vertex.import_build(catalog, load("training_manifest.json"), client=FakeVertex())
    used = {u["key"]: u["purpose"] for u in catalog.uses("staffing-forecast")["uses"]}
    assert used == {"table:workforce-demo.raw.shift_totals": "EDA of shift totals", FEATURE: "feature in v17"}
    assert [m["version"] for m in catalog.models("staffing-forecast")] == ["v17"] and catalog.models("other-model") == []


def test_explaining_what_is_not_there(catalog):
    with pytest.raises(CatalogError) as error:
        catalog.explain_model("staffing-forecast", "v17")
    assert error.value.code == "not_found"


def test_the_manifest_writer_hashes_the_artifact_it_describes(tmp_path):
    artifact = tmp_path / "model.bin"
    artifact.write_bytes(MODEL)
    written = manifest.build(model="staffing-forecast", version="v17", pipeline_run=EXPECTED["produced_by"]["pipeline_run"], task="train",
                             execution=EXPECTED["produced_by"]["execution"], training_commit=EXPECTED["produced_by"]["training_commit"],
                             prediction_cutoff="2026-09-01T00:00:00Z", dataset_version=EXPECTED["read"]["dataset_version"],
                             inputs=[{"field": "hours_at_cutoff", "role": "feature", "position": 0}],
                             artifact_uri="gs://demo-models/staffing/v17/model.bin", artifact_path=artifact, generation="1788318000000000")
    assert written == load("training_manifest.json")
    with pytest.raises(ValueError):
        manifest.build(**{**dict(model="m", version="v", pipeline_run="p", task="t", training_commit=None, prediction_cutoff=None,
                                 dataset_version="d", artifact_uri="u", artifact_path=artifact, generation="1"), "inputs": [{"field": "x", "role": "guess"}]})


def test_the_command_line_imports_a_build_and_explains_the_model(database, monkeypatch, capsys, tmp_path):
    from odcl.cli import main
    with Catalog(database) as catalog:
        build(catalog)
    monkeypatch.setattr(vertex, "client", lambda location: FakeVertex())
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(load("training_manifest.json")))
    db = ["--database-url", database]
    assert main([*db, "import", "--vertex-manifest", str(path)]) == 0 and "staffing-forecast v17  added" in capsys.readouterr().out
    assert main([*db, "import", "--vertex-manifest", str(path)]) == 0
    assert "v17  unchanged  (1 input(s), already recorded, so the artifact was not read again)" in capsys.readouterr().out
    assert main([*db, "--json", "explain", "model", "staffing-forecast", "--version", "v17"]) == 0
    trace = json.loads(capsys.readouterr().out)
    assert trace["inputs"][0]["derived_from"] == EXPECTED["inputs"][0]["derived_from"]
    assert main([*db, "explain", "model", "staffing-forecast", "--version", "v17"]) == 0
    out = capsys.readouterr().out
    assert "hours_at_cutoff = worked_minutes_asof / 60" in out and "train-042" in out and "workforce-demo.raw.shift_totals.worked_minutes_asof" in out


def test_the_real_client_reads_the_regional_endpoint_and_cloud_storage():
    from unittest.mock import Mock
    session = Mock()
    session.get.side_effect = [Mock(ok=True, json=Mock(return_value={"name": "j"})),
                               Mock(ok=True, json=Mock(return_value={"generation": "5", "size": "3"})),
                               Mock(ok=True, content=b"abc")]
    client = vertex.Client(session, "us-central1")
    client.get("projects/p/locations/us-central1/pipelineJobs/j")
    assert session.get.call_args.args[0] == "https://us-central1-aiplatform.googleapis.com/v1/projects/p/locations/us-central1/pipelineJobs/j"
    assert client.object("gs://bucket/dir/model.bin")["generation"] == "5"
    assert session.get.call_args.args[0] == "https://storage.googleapis.com/storage/v1/b/bucket/o/dir%2Fmodel.bin"
    assert client.read("gs://bucket/dir/model.bin", "5") == b"abc"
    assert session.get.call_args.kwargs["params"] == {"alt": "media", "generation": "5"}

    session.get.side_effect = [Mock(ok=True, json=Mock(return_value={"pipelineJobs": [{"name": "a"}], "nextPageToken": "t"})),
                               Mock(ok=True, json=Mock(return_value={"pipelineJobs": [{"name": "b"}]}))]
    assert [j["name"] for j in client.pipeline_jobs("p", vertex._time("2026-10-01T00:00:00Z"))] == ["a", "b"]  # every page
    assert session.get.call_args.args[0] == "https://us-central1-aiplatform.googleapis.com/v1/projects/p/locations/us-central1/pipelineJobs"
    assert session.get.call_args.kwargs["params"]["filter"] == 'create_time>"2026-10-01T00:00:00Z"' and session.get.call_args.kwargs["params"]["pageToken"] == "t"


def test_a_refusal_says_why_google_refused():
    from unittest.mock import Mock
    body = {"error": {"code": 403, "message": "The billing account for the owning project is disabled in state absent",
                      "errors": [{"reason": "accountDisabled"}]}}
    session = Mock(get=Mock(return_value=Mock(ok=False, status_code=403, json=Mock(return_value=body))))
    with pytest.raises(CatalogError) as error:
        vertex.Client(session, "us-central1").object("gs://bucket/model.bin")
    assert "billing account for the owning project is disabled" in str(error.value)
    assert "GOOGLE_CLOUD_QUOTA_PROJECT" in str(error.value)  # the quota project is the usual cause, and the fix is not obvious
    session.get.return_value.json.side_effect = ValueError("not json")
    with pytest.raises(CatalogError) as error:
        vertex.Client(session, "us-central1").object("gs://bucket/model.bin")
    assert "(403)" in str(error.value)


def test_a_project_lists_its_model_versions_and_the_project_list_says_which_are_models(catalog):
    build(catalog)
    catalog.put("project", {"name": "churn-eda", "description": "An analysis", "owner": "ana"})
    vertex.import_build(catalog, load("training_manifest.json"), client=FakeVertex())
    assert [m["version"] for m in catalog.get("staffing-forecast")["model_versions"]] == ["v17"]
    assert catalog.get("churn-eda")["model_versions"] == []
    projects = {p["key"]: p for p in catalog.projects()}
    assert projects["project:staffing-forecast"].items() >= {"uses": 1, "latest_version": "v17", "changed_since_used": 0}.items()
    assert projects["project:churn-eda"].items() >= {"uses": 0, "latest_version": None, "owner": "ana"}.items()


def test_replaying_a_version_recorded_before_projects_kept_their_uses_fills_them_in(catalog):
    build(catalog)
    vertex.import_build(catalog, load("training_manifest.json"), client=FakeVertex())
    catalog._use_from_model("project:staffing-forecast", [])  # as a version imported by an earlier release left it
    assert catalog.uses("staffing-forecast")["uses"] == []
    assert vertex.import_build(catalog, load("training_manifest.json"), client=FakeVertex())["change"] == "unchanged"
    assert [u["purpose"] for u in catalog.uses("staffing-forecast")["uses"]] == ["feature in v17"]


# ---- a model trained on files a data-loader task exported, not on a BigQuery snapshot ---------------------------------
FILE_JOB = json.loads((Path(__file__).parent / "fixtures" / "file_trace" / "vertex_pipeline_job.json").read_text())
LOADER_OUT = FILE_JOB["jobDetail"]["taskDetails"][0]["outputs"]["output_train_dataset"]["artifacts"][0]["uri"]
FILE_SET = f"{LOADER_OUT}@train-files-007"


def parquet_rows(**columns) -> bytes:
    pa = pytest.importorskip("pyarrow")
    import io

    import pyarrow.parquet as pq
    out = io.BytesIO()
    pq.write_table(pa.table(columns), out)
    return out.getvalue()


def exported(generation_offset=0, data=None):
    """Three Parquet parts and a _SUCCESS marker under the loader's output, and a sibling that only shares its prefix."""
    part = data or parquet_rows(employee_id=["E-1", "E-2"], hours_at_cutoff=[40.5, 38.0], tenure_days=[400, 30], is_train=[True, False])
    files = {f"{LOADER_OUT}/part-{i}.parquet": (part, str(1791000000000000 + i + generation_offset)) for i in range(3)}
    return files | {f"{LOADER_OUT}/_SUCCESS": (b"", "1791000000000009"), f"{LOADER_OUT}_old/part-0.parquet": (part, "1")}


def file_manifest(**changes):
    m = load("training_manifest.json") | {"version": "v20", "pipeline_run": FILE_JOB["name"], "task": "train", "dataset_version": FILE_SET,
                                          "execution": FILE_JOB["jobDetail"]["taskDetails"][1]["execution"]["name"]}
    m["artifact"] = m["artifact"] | {"uri": "gs://demo-models/staffing/v20/model.bin"}
    return m | changes


def files_client(objects=None):
    return FakeVertex(job=FILE_JOB, objects=(objects or exported()) | {"gs://demo-models/staffing/v20/model.bin": (MODEL, "1788318000000000")})


def record_loader_output(catalog, client=None, **options):
    return filesets.record(catalog, client=client or files_client(), pipeline_run=FILE_JOB["name"], task="load-data",
                           output="output_train_dataset", **options)


def test_a_file_set_a_task_exported_is_recorded_with_its_generations_and_fields(catalog):
    assert record_loader_output(catalog) == {"reference": FILE_SET, "change": "added", "objects": 3, "fields": 4, "schema": "read"}
    stored = catalog.db.execute("SELECT uri, generation FROM dataset_version_objects ORDER BY uri").fetchall()
    assert [(r["uri"].rsplit("/", 1)[1], r["generation"]) for r in stored] == [(f"part-{i}.parquet", str(1791000000000000 + i)) for i in range(3)]
    fields = {r["field_path"]: r["dtype"] for r in catalog.db.execute("SELECT field_path, dtype FROM version_columns")}
    assert fields == {"employee_id": "STRING", "hours_at_cutoff": "DOUBLE", "tenure_days": "INT64", "is_train": "BOOLEAN"}

    assert record_loader_output(catalog)["change"] == "unchanged"
    with pytest.raises(CatalogError) as error:  # the same reference cannot later name other bytes
        record_loader_output(catalog, client=files_client(exported(generation_offset=100)))
    assert error.value.code == "conflict" and "part-0.parquet" in str(error.value)


def test_a_model_trained_on_files_traces_to_the_files_their_producer_and_the_table_they_came_from(catalog):
    catalog.put("table", {"path": TARGET, "description": "Shift features", "system": "bigquery"})
    table_revision = catalog.get(TARGET)["revision"]
    record_loader_output(catalog, from_table=TARGET)
    assert vertex.import_build(catalog, file_manifest(), client=files_client())["change"] == "added"

    trace = catalog.explain_model("staffing-forecast", "v20")
    assert trace["read"] == {"dataset_version": FILE_SET, "kind": "gcs_files", "uri": LOADER_OUT, "objects": 3, "generations_pinned": True,
                             "produced_by": {"pipeline_run": FILE_JOB["name"], "task": "load-data", "state": "SUCCEEDED"},
                             "exported_from": {"table": TARGET, "revision": table_revision, "changed_since": False}}
    [feature] = trace["inputs"]
    assert feature == {"field": "hours_at_cutoff", "role": "feature", "position": 0, "dtype": "DOUBLE", "lineage": "not_claimed"}
    assert "column-level lineage below the file set is not claimed" in " ".join(trace["missing"])
    # the project depends on the table the files were exported from, at the revision pinned when they were recorded
    [used] = catalog.uses("staffing-forecast")["uses"]
    assert used["key"] == f"table:{TARGET}" and used["purpose"] == "read as files in v20"

    catalog.put("table", {"path": TARGET, "description": "Shift features, redefined", "system": "bigquery"})
    assert catalog.explain_model("staffing-forecast", "v20")["read"]["exported_from"]["changed_since"] is True


def test_a_file_replaced_or_deleted_since_it_was_recorded_is_named_and_nothing_is_written(catalog):
    record_loader_output(catalog)
    replaced = exported() | {f"{LOADER_OUT}/part-1.parquet": (exported()[f"{LOADER_OUT}/part-1.parquet"][0], "999")}
    deleted = {k: v for k, v in exported().items() if not k.endswith("part-2.parquet")}
    both = {k: v for k, v in replaced.items() if not k.endswith("part-2.parquet")}
    for objects, said in ((replaced, ["part-1.parquet was replaced"]), (deleted, ["part-2.parquet was deleted"]),
                          (both, ["part-1.parquet was replaced", "part-2.parquet was deleted"])):  # every change is named, not just the first
        with pytest.raises(CatalogError) as error:
            vertex.import_build(catalog, file_manifest(), client=files_client(objects))
        assert error.value.code == "dataset_files_changed" and all(s in str(error.value) for s in said)
    assert catalog.db.execute("SELECT count(*) AS n FROM model_builds").fetchone()["n"] == 0


def test_a_field_the_files_do_not_have_or_files_whose_schema_cannot_be_read_are_refused_with_the_reason(catalog):
    record_loader_output(catalog)
    wrong = file_manifest(inputs=[{"field": "hours_worked", "role": "feature", "position": 0}])
    with pytest.raises(CatalogError) as error:
        vertex.import_build(catalog, wrong, client=files_client())
    assert error.value.code == "missing_dependency" and "hours_worked" in str(error.value)

    not_parquet = {f"{LOADER_OUT}/part-0.csv": (b"employee_id,hours\nE-1,40\n", "5")}
    other = FILE_JOB["name"].replace("train-files-007", "train-files-008")
    job = json.loads(json.dumps(FILE_JOB).replace("train-files-007", "train-files-008"))
    out = LOADER_OUT.replace("train-files-007", "train-files-008")
    client = FakeVertex(job=job, objects={k.replace("train-files-007", "train-files-008"): v for k, v in not_parquet.items()}
                        | {"gs://demo-models/staffing/v20/model.bin": (MODEL, "1788318000000000")})
    result = filesets.record(catalog, client=client, pipeline_run=other, task="load-data", output="output_train_dataset")
    assert result["fields"] == 0 and "no Parquet object" in result["schema"]
    with pytest.raises(CatalogError) as error:
        vertex.import_build(catalog, file_manifest(dataset_version=f"{out}@train-files-008", pipeline_run=other), client=client)
    assert error.value.code == "missing_dependency" and "no Parquet object" in str(error.value)


def test_files_named_directly_have_no_producer_and_the_trace_says_so(catalog):
    result = filesets.record(catalog, client=files_client(), uris=[LOADER_OUT])
    assert result["reference"] == f"{LOADER_OUT}@1791000000000002" and result["objects"] == 3  # named by its newest object
    trace = builds.explain_dataset(catalog, result["reference"])
    assert trace["produced_by"] is None and trace["exported_from"] is None
    assert any("producer" in m for m in trace["missing"]) and any("exported from" in m for m in trace["missing"])
    verification = catalog.db.execute("SELECT verification FROM dataset_versions").fetchone()["verification"]
    assert verification["producer_unknown"] is True


def test_a_loader_task_that_failed_or_has_no_such_output_records_nothing(catalog):
    failed = json.loads(json.dumps(FILE_JOB))
    failed["jobDetail"]["taskDetails"][0]["state"] = "FAILED"
    with pytest.raises(CatalogError) as error:
        record_loader_output(catalog, client=FakeVertex(job=failed, objects=exported()))
    assert error.value.code == "task_not_succeeded"
    with pytest.raises(CatalogError) as error:
        filesets.record(catalog, client=files_client(), pipeline_run=FILE_JOB["name"], task="load-data", output="output_test_dataset")
    assert error.value.code == "output_not_found" and "output_train_dataset" in str(error.value)
    assert catalog.db.execute("SELECT count(*) AS n FROM dataset_versions").fetchone()["n"] == 0


def test_the_command_line_records_files_and_explains_them(database, monkeypatch, capsys):
    from odcl.cli import main
    monkeypatch.setattr(vertex, "client", lambda location: files_client())
    db = ["--database-url", database]
    assert main([*db, "record", "dataset-files", "--pipeline-run", FILE_JOB["name"], "--task", "load-data", "--output", "output_train_dataset"]) == 0
    assert f"{FILE_SET}  added  (3 objects, 4 fields)" in capsys.readouterr().out
    assert main([*db, "explain", "dataset", FILE_SET]) == 0
    out = capsys.readouterr().out
    assert "3 objects, generations pinned" in out and "load-data" in out and "hours_at_cutoff  DOUBLE" in out


def test_a_model_version_pins_the_revision_of_the_finding_it_relied_on_and_a_correction_shows(catalog, capsys):
    build(catalog)
    finding = catalog.put("document", {"repository": "kb", "path": "findings/hours.md", "title": "Hours lag a day", "digest": "a" * 64,
                                       "text": "Hours land a day late."})["key"]
    catalog.db.execute("UPDATE revisions SET recorded_at = '2026-09-01T00:00:00Z' WHERE entry_key = %s", (finding,))  # read before training
    old = catalog.get(finding)["revision"]
    catalog.put("document", {"repository": "kb", "path": "findings/hours.md", "title": "Hours lag two days", "digest": "b" * 64,
                             "text": "Corrected: two days."})
    glossary = catalog.put("document", {"repository": "kb", "path": "glossary.md", "digest": "c" * 64})["key"]

    trained = load("training_manifest.json") | {"relied_on": [{"document": finding, "why": "shift the label by a day"},
                                                               {"document": glossary, "digest": "c" * 64}]}
    vertex.import_build(catalog, trained, client=FakeVertex())
    relied = {d["document"]: d for d in catalog.explain_model("staffing-forecast", "v17")["relied_on"]}
    assert relied[finding] | {} == {"document": finding, "title": "Hours lag two days", "revision": old, "pinned_by": "at_training",
                                    "why": "shift the label by a day", "changed_since": True}  # it was built on the old text
    assert (relied[glossary]["pinned_by"], relied[glossary]["changed_since"]) == ("digest", False)
    assert catalog.overview()["changed_since_relied_on"] == [{"model": "staffing-forecast", "version": "v17", "document": finding}]
    [by] = catalog.get(finding)["relied_on_by"]
    assert (by["model"], by["version"], by["revision"], by["changed_since"]) == ("staffing-forecast", "v17", old, True)
    assert vertex.import_build(catalog, trained, client=FakeVertex())["change"] == "unchanged"

    with pytest.raises(CatalogError, match="Nothing registered as document:kb/missing.md"):
        vertex.import_build(catalog, load("training_manifest.json") | {"version": "v18", "relied_on": ["document:kb/missing.md"]}, client=FakeVertex())
    assert not catalog.db.execute("SELECT 1 FROM model_builds WHERE version = 'v18'").fetchone()  # refused before anything was written

    later = catalog.put("document", {"repository": "kb", "path": "runbook.md"})["key"]  # registered after training: pinned as it is now
    [added] = [d for d in catalog.rely("staffing-forecast", "v17", [later], why="retraining steps")["relied_on"] if d["document"] == later]
    assert added["pinned_by"] == "current" and added["why"] == "retraining steps"


def test_a_run_is_discovered_and_its_proposed_manifest_imports_once_its_files_are_recorded(catalog, capsys, database, monkeypatch, tmp_path):
    from odcl import discover
    from odcl.cli import main
    job = json.loads(json.dumps(FILE_JOB))
    trainer = job["jobDetail"]["taskDetails"][1]
    trainer["outputs"] = {"model": {"artifacts": [{"uri": "gs://demo-models/staffing/v20", "schemaTitle": "system.Model"}]}}
    metadata = {"model_name": "staffing-forecast", "version": "v20", "git_commit": "90c19d9d6f10b60919d40d73eb4d7a60c9fdcd7f",
                "feature_columns": ["hours_at_cutoff", "tenure_days"], "label_column": "is_train"}
    client = FakeVertex(job=job, objects=exported() | {"gs://demo-models/staffing/v20/model.bin": (MODEL, "1788318000000000"),
                                                       "gs://demo-models/staffing/v20/metadata.json": (json.dumps(metadata).encode(), "5")})

    draft = discover.propose(catalog, client, job["name"])
    manifest = draft["manifest"]
    assert (manifest["model"], manifest["version"], manifest["task"], manifest["execution"]) == (
        "staffing-forecast", "v20", "train", trainer["execution"]["name"])
    assert manifest["inputs"] == [{"field": "hours_at_cutoff", "role": "feature", "position": 0}, {"field": "tenure_days", "role": "feature", "position": 1},
                                  {"field": "is_train", "role": "label"}]
    assert manifest["artifact"] == {"uri": "gs://demo-models/staffing/v20/model.bin", "generation": "1788318000000000", "sha256": hashlib.sha256(MODEL).hexdigest()}
    assert manifest["dataset_version"] == FILE_SET and draft["sources"]["inputs"] == "gs://demo-models/staffing/v20/metadata.json"
    assert not draft["ready"] and "odcl record dataset-files" in " ".join(draft["review"])  # the files are not recorded yet

    record_loader_output(catalog, client=client)
    draft = discover.propose(catalog, client, job["name"])
    assert draft["ready"] and draft["review"] == [] and draft["missing"] == []
    assert vertex.import_build(catalog, draft["manifest"], client=client)["change"] == "added"  # the draft passes every check

    client.pipeline_jobs = lambda project, since: [job, {"name": "projects/workforce-demo/locations/us-central1/pipelineJobs/other", "state": "PIPELINE_STATE_RUNNING"}]
    found = {r["pipeline_run"]: r["model_versions"] for r in discover.runs(catalog, client, "workforce-demo", vertex._time("2026-10-01T00:00:00Z"))}
    assert found == {job["name"]: ["staffing-forecast v20"], "projects/workforce-demo/locations/us-central1/pipelineJobs/other": []}

    bare = json.loads(json.dumps(FILE_JOB))  # no Model output and no metadata.json: everything to fill in is named
    draft = discover.propose(catalog, FakeVertex(job=bare, objects=exported()), bare["name"])
    assert not draft["ready"] and {"model", "task", "artifact", "inputs"} <= set(draft["missing"])

    monkeypatch.setattr(vertex, "client", lambda location: client)
    out = tmp_path / "manifest.json"
    assert main(["--database-url", database, "propose", "vertex-manifest", "--pipeline-run", job["name"], "--out", str(out)]) == 0
    assert "Ready to import." in capsys.readouterr().out and json.loads(out.read_text())["version"] == "v20"
    assert main(["--database-url", database, "discover", "vertex-runs", "--project", "workforce-demo", "--since", "2026-10-01"]) == 0
    assert "recorded: staffing-forecast v20" in capsys.readouterr().out
