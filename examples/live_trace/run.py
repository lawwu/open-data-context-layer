"""Run the live trace test: a real Dataform build, a snapshot handoff, and a Vertex AI training run that reads it.

    ODCL_LIVE_PROJECT=my-sandbox-project python examples/live_trace/run.py dataform
    ODCL_LIVE_PROJECT=my-sandbox-project uv run --with "kfp>=2.7,<3" python examples/live_trace/run.py vertex SNAPSHOT_REFERENCE VERSION

`dataform` writes 20 synthetic rows, runs the Dataform project in `dataform/` once as the test service account, and
takes a uniquely named snapshot of the table it built. `vertex` trains a trivial model on that snapshot in a
one-step Vertex AI pipeline, which uploads the model and a training manifest. Each prints the `odcl` commands
that import and explain what it made. The BigQuery work is a few kilobytes; the pipeline runs for a few minutes.
"""
# No `from __future__ import annotations`: kfp reads the component's parameter types and takes "str" for an artifact.
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import google.auth
from google.auth.transport.requests import AuthorizedSession

PROJECT = os.environ.get("ODCL_LIVE_PROJECT") or sys.exit("Set ODCL_LIVE_PROJECT to the sandbox project setup.sh prepared")
REGION = os.environ.get("ODCL_LIVE_REGION", "us-central1")
REPOSITORY = f"projects/{PROJECT}/locations/{REGION}/repositories/odcl-live-test"
WORKSPACE = f"{REPOSITORY}/workspaces/odcl-dev"
DATAFORM_ACCOUNT = f"odcl-dataform-test@{PROJECT}.iam.gserviceaccount.com"
VERTEX_ACCOUNT = f"odcl-vertex-test@{PROJECT}.iam.gserviceaccount.com"
BUCKET = f"{PROJECT}-odcl-live"
HERE = Path(__file__).parent
credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
session = AuthorizedSession(credentials)


def call(method: str, url: str, **kwargs) -> dict:
    response = session.request(method, url, timeout=120, **kwargs)
    if not response.ok:
        sys.exit(f"{method} {url} -> {response.status_code}\n{response.text[:1500]}")
    return response.json() if response.text else {}


def dataform_api(method: str, name: str, **kwargs) -> dict:
    return call(method, f"https://dataform.googleapis.com/v1/{name}", **kwargs)


def dataform() -> None:
    from google.cloud import bigquery
    bq = bigquery.Client(project=PROJECT)
    bq.query(f"""CREATE OR REPLACE TABLE `{PROJECT}.odcl_demo_raw.shift_totals`
        (employee_id STRING NOT NULL, pay_period_start DATE NOT NULL,
         worked_minutes_asof INT64 OPTIONS (description = "Minutes worked in the pay period, as known at load time"))
        OPTIONS (description = "Synthetic shift totals for the catalog's live test") AS
        SELECT CONCAT('E-', CAST(n AS STRING)), DATE '2026-08-17', 2400 + n * 7 FROM UNNEST(GENERATE_ARRAY(1, 20)) AS n""",
             job_id_prefix="odcl-live-setup-").result()

    if session.get(f"https://dataform.googleapis.com/v1/{REPOSITORY}").status_code == 404:
        dataform_api("POST", REPOSITORY.rsplit("/repositories/", 1)[0] + "/repositories", params={"repositoryId": "odcl-live-test"},
                     json={"displayName": "odcl live trace test"})
    if session.get(f"https://dataform.googleapis.com/v1/{WORKSPACE}").status_code == 404:
        dataform_api("POST", f"{REPOSITORY}/workspaces", params={"workspaceId": "odcl-dev"}, json={})
    for path in sorted(p for p in (HERE / "dataform").rglob("*") if p.is_file()):
        text = path.read_text().replace("PROJECT", PROJECT)
        dataform_api("POST", f"{WORKSPACE}:writeFile", json={"path": path.relative_to(HERE / "dataform").as_posix(),
                                                              "contents": base64.b64encode(text.encode()).decode()})
    compiled = dataform_api("POST", f"{REPOSITORY}/compilationResults", json={"workspace": WORKSPACE})
    if compiled.get("compilationErrors"):
        sys.exit(json.dumps(compiled["compilationErrors"], indent=2))
    invocation = dataform_api("POST", f"{REPOSITORY}/workflowInvocations", json={
        "compilationResult": compiled["name"], "invocationConfig": {"serviceAccount": DATAFORM_ACCOUNT}})
    print("invocation", invocation["name"], flush=True)
    while invocation["state"] in ("RUNNING", "CANCELING"):
        time.sleep(5)
        invocation = dataform_api("GET", invocation["name"])
    actions = dataform_api("GET", f"{invocation['name']}:query").get("workflowInvocationActions", [])
    for action in actions:
        print(" ", ".".join(action["target"].values()), action["state"], (action.get("bigqueryAction") or {}).get("jobId"), action.get("failureReason", ""))
    if invocation["state"] != "SUCCEEDED":
        sys.exit(f"The invocation {invocation['state']}")

    build = invocation["name"].rsplit("/", 1)[1].split("-")[1][:8]  # ids are <seconds>-<uuid>; the uuid is what is unique
    snapshot = f"{PROJECT}.odcl_demo_frozen.shift_inputs_{build}"
    bq.query(f"CREATE SNAPSHOT TABLE `{snapshot}` CLONE `{PROJECT}.odcl_demo_features.shift_inputs`", job_id_prefix="odcl-live-handoff-").result()
    taken = bq.get_table(snapshot)._properties["snapshotDefinition"]["snapshotTime"]
    print(f"""
snapshot {snapshot} taken at {taken}

odcl sync bigquery-jobs --project {PROJECT} --backfill 1d
odcl import --bigquery {PROJECT}.odcl_demo_raw.shift_totals {PROJECT}.odcl_demo_features.shift_inputs --project {PROJECT}
odcl import --dataform {invocation['name']}
odcl handoff --invocation {invocation['name']} --target {PROJECT}.odcl_demo_features.shift_inputs --snapshot {snapshot} --project {PROJECT}
odcl explain dataset {snapshot}""")


def vertex(reference: str, version: str) -> None:
    from kfp import compiler, dsl

    @dsl.component(base_image="python:3.12-slim", packages_to_install=["google-cloud-bigquery>=3.25", "google-cloud-storage>=2.18"])
    def train(dataset_version: str, model: str, version: str, prediction_cutoff: str, model_uri: str, manifest_uri: str,
              pipeline_run: str, task: str) -> str:
        """Fit a trivial model on the snapshot the build handed on, upload it, then write the training manifest."""
        import hashlib
        import json
        import pickle

        from google.cloud import bigquery, storage

        table = dataset_version.split("@", 1)[0]
        rows = list(bigquery.Client(project=table.split(".")[0]).list_rows(table))
        features = ["hours_at_cutoff"]  # the columns of the matrix as fitted, read from it rather than from configuration
        matrix = [[row[f] for f in features] for row in rows]
        fitted = {"mean": [sum(r[i] for r in matrix) / len(matrix) for i in range(len(features))], "rows": len(matrix)}
        data = pickle.dumps(fitted)

        def blob(uri):
            bucket, path = uri.removeprefix("gs://").split("/", 1)
            return storage.Client().bucket(bucket).blob(path)

        artifact = blob(model_uri)
        artifact.upload_from_string(data, if_generation_match=0)  # a model version is written once
        artifact.reload()
        manifest = {"schema_version": 1, "model": model, "version": version, "pipeline_run": pipeline_run, "task": task,
                    "training_commit": None, "prediction_cutoff": prediction_cutoff, "dataset_version": dataset_version,
                    "inputs": [{"field": f, "role": "feature", "position": i} for i, f in enumerate(features)],
                    "artifact": {"uri": model_uri, "generation": str(artifact.generation), "sha256": hashlib.sha256(data).hexdigest()}}
        blob(manifest_uri).upload_from_string(json.dumps(manifest, indent=2), content_type="application/json", if_generation_match=0)
        return manifest_uri

    @dsl.pipeline(name="odcl-live-trace")
    def pipeline(dataset_version: str, model: str, version: str, prediction_cutoff: str, model_uri: str, manifest_uri: str):
        train(dataset_version=dataset_version, model=model, version=version, prediction_cutoff=prediction_cutoff, model_uri=model_uri,
              manifest_uri=manifest_uri, pipeline_run=dsl.PIPELINE_JOB_RESOURCE_NAME_PLACEHOLDER, task=dsl.PIPELINE_TASK_NAME_PLACEHOLDER)

    spec = Path("/tmp/odcl-live-pipeline.json")
    compiler.Compiler().compile(pipeline, str(spec))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    model_uri = f"gs://{BUCKET}/models/staffing-forecast/{version}-{stamp}/model.bin"
    manifest_uri = model_uri.rsplit("/", 1)[0] + "/manifest.json"
    job = call("POST", f"https://{REGION}-aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/pipelineJobs",
               params={"pipelineJobId": f"odcl-live-{version}-{stamp}"}, json={
                   "displayName": f"odcl live trace {version}", "pipelineSpec": json.loads(spec.read_text()), "serviceAccount": VERTEX_ACCOUNT,
                   "runtimeConfig": {"gcsOutputDirectory": f"gs://{BUCKET}/pipeline-root", "parameterValues": {
                       "dataset_version": reference, "model": "staffing-forecast", "version": version, "prediction_cutoff": "2026-09-01T00:00:00Z",
                       "model_uri": model_uri, "manifest_uri": manifest_uri}}})
    print("pipeline job", job["name"], flush=True)
    while job["state"] not in ("PIPELINE_STATE_SUCCEEDED", "PIPELINE_STATE_FAILED", "PIPELINE_STATE_CANCELLED"):
        time.sleep(20)
        job = call("GET", f"https://{REGION}-aiplatform.googleapis.com/v1/{job['name']}")
        print(" ", job["state"], flush=True)
    if job["state"] != "PIPELINE_STATE_SUCCEEDED":
        sys.exit(json.dumps(job.get("error"), indent=2))
    print(f"""
odcl import --vertex-manifest {manifest_uri}
odcl explain model staffing-forecast --version {version}""")


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else ""
    if step == "dataform":
        dataform()
    elif step == "vertex" and len(sys.argv) == 4:
        vertex(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
