"""A model version, from its Vertex AI training task back to the raw column behind each feature.

`expected_explain.json` is the independent statement of the whole trace; the first test checks the answer against
it field for field.
"""
import hashlib
import json
from pathlib import Path

import pytest

from odcl import Catalog, CatalogError, builds, dataform, manifest, vertex
from test_dataform import INVOCATION, TARGET, FakeDataform, load, snapshot_facts, sync_history

FIXTURES = Path(__file__).parent / "fixtures" / "staffing_trace"
EXPECTED = json.loads((FIXTURES / "expected_explain.json").read_text())
MODEL = (FIXTURES / "model.bin").read_bytes()


class FakeVertex:
    """Answers the pipeline-job read and the two Cloud Storage reads from the fixture."""

    def __init__(self, job=None, objects=None):
        self.job = job or load("vertex_pipeline_job.json")
        self.objects = objects or {"gs://demo-models/staffing/v17/model.bin": (MODEL, "1788318000000000")}

    def get(self, name):
        assert name == self.job["name"], name
        return self.job

    def object(self, uri):
        data, generation = self.objects[uri]
        return {"generation": generation, "size": str(len(data))}

    def read(self, uri, generation):
        data, current = self.objects[uri]
        assert generation == current
        return data


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
    assert catalog.get("project:staffing-forecast")["kind"] == "project"  # the model family is a project that uses data


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
