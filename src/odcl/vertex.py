"""Vertex AI Pipelines into the catalog: one model version, the training task that produced it, and what it read.

The training task writes a manifest (see `manifest`). Importing it checks every claim before anything is written:
the pipeline run and task exist and the task succeeded, the artifact in Cloud Storage is the object generation
the manifest names and hashes to its SHA-256, the dataset version it read is recorded, and each field it names
is a column of that version. Then the run, the task and the build are written in one transaction.

Reads are free: `pipelineJobs.get` and Cloud Storage object reads.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from urllib.parse import quote

from psycopg.types.json import Jsonb

from .builds import reference_key
from .gcp import refusal
from .store import Catalog, CatalogError, require

JOB = re.compile(r"^projects/([^/]+)/locations/([^/]+)/pipelineJobs/([^/]+)$")
GCS = re.compile(r"^gs://([^/]+)/(.+)$")
MAX_HASHED = 512 * 1024 * 1024  # an artifact larger than this is checked by generation only
SUCCEEDED = ("SUCCEEDED",)
REQUIRED = ("schema_version", "model", "version", "pipeline_run", "task", "dataset_version", "inputs", "artifact")


class Client:
    """The Vertex AI and Cloud Storage reads, over an authorized HTTP session."""

    def __init__(self, session, location: str):
        self.session, self.location = session, location

    def _get(self, url: str, headers: dict | None = None, missing_ok: bool = False, **params):
        response = self.session.get(url, params=params or None, timeout=120, **({"headers": headers} if headers else {}))
        if missing_ok and getattr(response, "status_code", None) == 404:
            return None
        if not response.ok:
            raise CatalogError("vertex", f"Google refused the request for {url.split('?')[0]}: {refusal(response)}")
        return response

    def get(self, name: str) -> dict:
        return self._get(f"https://{self.location}-aiplatform.googleapis.com/v1/{name}").json()

    def object(self, uri: str, missing_ok: bool = False) -> dict | None:
        """An object's metadata, or None when `missing_ok` and it is not there."""
        bucket, path = GCS.match(uri).groups()
        found = self._get(f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quote(path, safe='')}", missing_ok=missing_ok)
        return found.json() if found is not None else None

    def list(self, prefix: str) -> list[dict]:
        """Every object whose name starts with the path of `prefix`, following page tokens."""
        bucket, path = GCS.match(prefix).groups()
        found, token = [], None
        while True:
            page = self._get(f"https://storage.googleapis.com/storage/v1/b/{bucket}/o", prefix=path,
                             fields="items(name,bucket,generation,size,crc32c,md5Hash),nextPageToken", **({"pageToken": token} if token else {})).json()
            found += page.get("items") or []
            token = page.get("nextPageToken")
            if not token:
                return found

    def tail(self, uri: str, generation: str, size: int) -> bytes:
        """The last `size` bytes of one generation of an object."""
        bucket, path = GCS.match(uri).groups()
        return self._get(f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quote(path, safe='')}", headers={"Range": f"bytes=-{size}"},
                         alt="media", generation=generation).content

    def read(self, uri: str, generation: str) -> bytes:
        bucket, path = GCS.match(uri).groups()
        return self._get(f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quote(path, safe='')}", alt="media", generation=generation).content


def client(location: str) -> Client:
    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
    except ImportError as e:  # pragma: no cover - depends on the installation
        raise CatalogError("vertex", "The Vertex AI importer needs google-auth: install open-data-context-layer[vertex]") from e
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return Client(AuthorizedSession(credentials), location)


def read_manifest(source: str, client: Client | None = None) -> dict:
    """A manifest from a local file or a gs:// object."""
    if source.startswith("gs://"):
        require(client is not None, "manifest", "Reading a manifest from Cloud Storage needs a client")
        return json.loads(client.read(source, client.object(source)["generation"]))
    with open(source, encoding="utf-8") as f:
        return json.load(f)


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _execution(db, row: dict) -> int:
    return db.execute("""
        INSERT INTO executions (system, native_key, kind, parent_id, state, started_at, ended_at, evidence)
        VALUES ('vertex', %(native_key)s, %(kind)s, %(parent_id)s, %(state)s, %(started_at)s, %(ended_at)s, %(evidence)s)
        ON CONFLICT (system, native_key) DO UPDATE SET
          state = CASE WHEN executions.state IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'SKIPPED', 'PIPELINE_STATE_SUCCEEDED',
                                                 'PIPELINE_STATE_FAILED', 'PIPELINE_STATE_CANCELLED') THEN executions.state ELSE excluded.state END,
          started_at = COALESCE(executions.started_at, excluded.started_at), ended_at = COALESCE(executions.ended_at, excluded.ended_at),
          evidence = excluded.evidence || executions.evidence, observed_at = now()
        RETURNING id""", row).fetchone()["id"]


def import_build(catalog: Catalog, manifest: dict, *, client) -> dict:
    """Check a training manifest against Google and the catalog, then record the model version it describes.

    Importing the same manifest again changes nothing. A different manifest for a version already recorded is
    refused. A missing dependency is named, and nothing is written until it is there."""
    require(isinstance(manifest, dict) and all(k in manifest for k in REQUIRED), "manifest", f"A manifest needs {', '.join(REQUIRED)}")
    require(manifest["schema_version"] == 1, "manifest", f"This release reads manifest schema version 1, not {manifest['schema_version']}")
    found = JOB.match(manifest["pipeline_run"] or "")
    require(found, "manifest", "pipeline_run is a Vertex AI pipeline job: projects/P/locations/L/pipelineJobs/J")
    model, version, artifact = manifest["model"], str(manifest["version"]), manifest["artifact"]
    require(GCS.match(artifact.get("uri") or "") and re.fullmatch(r"[0-9a-f]{64}", artifact.get("sha256") or ""), "manifest",
            "artifact needs a gs:// uri and a SHA-256")

    canonical = json.dumps(manifest, sort_keys=True)
    said = hashlib.sha256(canonical.encode()).hexdigest()
    project = f"project:{model.lower()}"
    recorded = catalog.db.execute("SELECT id, manifest_sha256 FROM model_builds WHERE project_key = %s AND version = %s", (project, version)).fetchone()
    if recorded:  # a version is built once: a replay needs no checking, and anything else is refused before reading Google
        require(recorded["manifest_sha256"] == said, "conflict", f"{model} {version} is already recorded from a different manifest; a version is built once")
        with catalog.db.transaction():
            _record_use(catalog, recorded["id"])  # versions recorded before projects kept their uses get them on replay
        return {"model": model, "version": version, "change": "unchanged", "inputs": len(manifest["inputs"]), "artifact_verified": "recorded"}

    # the run and the task that claims to have produced the model
    job = client.get(manifest["pipeline_run"])
    task = task_detail(job, manifest["pipeline_run"], manifest["task"])
    require(task.get("state") in SUCCEEDED, "task_not_succeeded", f"Task {manifest['task']} is {task.get('state')}, so it produced no model")
    execution = (task.get("execution") or {}).get("name")
    require(not manifest.get("execution") or manifest["execution"] == execution, "execution_mismatch",
            f"The manifest names execution {manifest.get('execution')}, but task {manifest['task']} ran as {execution}")

    # the artifact is the object the manifest names, unchanged
    stored = client.object(artifact["uri"])
    require(not artifact.get("generation") or str(stored.get("generation")) == str(artifact["generation"]), "artifact_replaced",
            f"{artifact['uri']} is now generation {stored.get('generation')}, not {artifact.get('generation')}: the object was replaced")
    verified = "generation"
    if int(stored.get("size") or 0) <= MAX_HASHED:
        digest = hashlib.sha256(client.read(artifact["uri"], str(stored.get("generation")))).hexdigest()
        require(digest == artifact["sha256"], "artifact_digest", f"{artifact['uri']} hashes to {digest}, not the manifest's {artifact['sha256']}")
        verified = "sha256"

    # what it read, as the catalog recorded it
    db = catalog.db
    reference = reference_key(manifest["dataset_version"])
    data = db.execute("SELECT id, kind, verification FROM dataset_versions WHERE reference = %s", (reference,)).fetchone()
    require(data, "missing_dependency", f"No dataset version {reference} is recorded. Record the build's handoff (odcl handoff) or the files it read "
                                        "(odcl record dataset-files), then import again")
    if data["kind"] == "gcs_files":  # the files must still be the bytes recorded, or the model cannot be shown to have read them
        changed = []
        for o in db.execute("SELECT uri, generation FROM dataset_version_objects WHERE dataset_version_id = %s ORDER BY uri", (data["id"],)).fetchall():
            now = client.object(o["uri"], missing_ok=True)
            if now is None:
                changed.append(f"{o['uri']} was deleted")
            elif str(now.get("generation")) != o["generation"]:
                changed.append(f"{o['uri']} was replaced (generation {o['generation']} is now {now.get('generation')})")
        require(not changed, "dataset_files_changed", f"Since {reference} was recorded, " + "; ".join(changed)
                + ". The files the model read can no longer be shown to be these")
    fields = {r["field_path"] for r in db.execute("SELECT field_path FROM version_columns WHERE dataset_version_id = %s", (data["id"],))}
    absent = [i["field"] for i in manifest["inputs"] if i["field"].lower() not in fields]
    reason = data["verification"].get("schema", "read")
    require(not absent, "missing_dependency", f"{reference} has no recorded field {', '.join(absent)}" + (f": {reason}" if not fields and reason != "read" else ""))

    with db.transaction():
        catalog._lock(f"{project}@{version}")
        existing = db.execute("SELECT manifest_sha256 FROM model_builds WHERE project_key = %s AND version = %s", (project, version)).fetchone()
        if existing:
            require(existing["manifest_sha256"] == said, "conflict", f"{model} {version} is already recorded from a different manifest; a version is built once")
            return {"model": model, "version": version, "change": "unchanged", "inputs": len(manifest["inputs"]), "artifact_verified": verified}
        if not catalog._head(project):
            catalog.put("project", {"name": model, "description": f"Model {model}, trained in Vertex AI Pipelines",
                                    "source": {"vertex": {"created_by": "the import of its first model version"}}})
        producer = record_task(db, job, task, manifest["pipeline_run"], manifest["task"])
        build = db.execute("""
            INSERT INTO model_builds (project_key, version, producing_execution, dataset_version_id, artifact_uri, artifact_generation, artifact_sha256,
                                      training_commit, prediction_cutoff, manifest, manifest_sha256, recorded_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (project, version, producer, data["id"], artifact["uri"], str(stored.get("generation")), artifact["sha256"], manifest.get("training_commit"),
             _time(manifest.get("prediction_cutoff")), Jsonb(manifest), said, catalog.actor)).fetchone()["id"]
        with db.cursor() as cursor:
            cursor.executemany("INSERT INTO build_inputs (build_id, role, field, position, dataset_version_id, field_path) VALUES (%s, %s, %s, %s, %s, %s)",
                               [(build, i["role"], i["field"], i.get("position"), data["id"], i["field"].lower()) for i in manifest["inputs"]])
        _record_use(catalog, build)
    return {"model": model, "version": version, "change": "added", "inputs": len(manifest["inputs"]), "artifact_verified": verified}


def task_detail(job: dict, pipeline_run: str, name: str) -> dict:
    task = next((t for t in (job.get("jobDetail") or {}).get("taskDetails") or [] if t.get("taskName") == name), None)
    require(task, "task_not_found", f"{pipeline_run} has no task named {name}")
    return task


def record_task(db, job: dict, task: dict, pipeline_run: str, name: str) -> int:
    """Record a pipeline run and one of its tasks as executions; the task's id."""
    run = _execution(db, {"native_key": pipeline_run, "kind": "pipeline_run", "parent_id": None, "state": job.get("state") or "UNKNOWN",
                          "started_at": _time(job.get("startTime")), "ended_at": _time(job.get("endTime")),
                          "evidence": Jsonb({"display_name": job.get("displayName")})})
    execution = (task.get("execution") or {}).get("name")
    return _execution(db, {"native_key": execution or f"{pipeline_run}/tasks/{name}", "kind": "task", "parent_id": run,
                           "state": task["state"], "started_at": _time(task.get("startTime")), "ended_at": _time(task.get("endTime")),
                           "evidence": Jsonb({"task": name, "task_id": task.get("taskId"), "pipeline_run": pipeline_run})})


def _record_use(catalog: Catalog, build: int) -> None:
    """If `build` is its project's most recently trained version, point the project's `uses` at the columns it
    was trained on, pinned to the definitions that version used, so `used_by` and changed-since reach the model."""
    db = catalog.db
    row = db.execute("""SELECT b.project_key, b.version, t.ended_at FROM model_builds b JOIN executions t ON t.id = b.producing_execution
                        WHERE b.id = %s""", (build,)).fetchone()
    later = db.execute("""SELECT 1 FROM model_builds b JOIN executions t ON t.id = b.producing_execution
                          WHERE b.project_key = %s AND b.id <> %s AND t.ended_at > %s""", (row["project_key"], build, row["ended_at"])).fetchone() \
        if row["ended_at"] else None
    if later:
        return
    pinned = db.execute("""SELECT i.role, c.column_key, c.column_revision FROM build_inputs i
                           JOIN version_columns c ON (c.dataset_version_id, c.field_path) = (i.dataset_version_id, i.field_path)
                           WHERE i.build_id = %s AND c.column_key IS NOT NULL ORDER BY c.column_key, i.role""", (build,)).fetchall()
    uses = [(r["column_key"], r["column_revision"], f"{r['role']} in {row['version']}") for r in pinned]
    files = db.execute("""SELECT d.table_key, d.table_revision FROM model_builds b JOIN dataset_versions d ON d.id = b.dataset_version_id
                          WHERE b.id = %s AND d.kind = 'gcs_files' AND d.table_key IS NOT NULL""", (build,)).fetchone()
    if files:  # a file set's fields pin no column; the table it was exported from is what the model depends on
        uses.append((files["table_key"], files["table_revision"], f"read as files in {row['version']}"))
    catalog._use_from_model(row["project_key"], uses)


def explain_model(catalog: Catalog, name: str, version: str) -> dict:
    """Everything recorded about one model version: the task that trained it, its artifact, the dataset version it
    read, and for each input field, the build, job, file, expression and raw columns behind it, all as pinned
    when it was trained. `missing` names every link that could not be established."""
    from .builds import explain_dataset
    db = catalog.db
    row = db.execute("""SELECT b.*, d.reference, t.native_key AS execution, t.evidence AS task FROM model_builds b
                        JOIN dataset_versions d ON d.id = b.dataset_version_id JOIN executions t ON t.id = b.producing_execution
                        WHERE b.project_key = %s AND b.version = %s""", (f"project:{name.strip().lower()}", str(version))).fetchone()
    require(row, "not_found", f"No build of {name} {version} is recorded. Import its training manifest with odcl import --vertex-manifest")
    data = explain_dataset(catalog, row["reference"])
    columns = {c["field"]: c for c in data["columns"]}
    manifest = row["manifest"]
    produced = {"pipeline_run": row["task"]["pipeline_run"], "task": row["task"]["task"], "execution": row["execution"],
                "training_commit": row["training_commit"], "prediction_cutoff": manifest.get("prediction_cutoff")}
    artifact = {"uri": row["artifact_uri"], "generation": row["artifact_generation"], "sha256": row["artifact_sha256"]}
    if data.get("kind") == "gcs_files":
        return {
            "model": manifest["model"], "version": row["version"], "produced_by": produced, "artifact": artifact,
            "read": {"dataset_version": data["reference"], "kind": "gcs_files", "uri": data["uri"], "objects": len(data["objects"]), "generations_pinned": True,
                     "produced_by": {k: data["produced_by"][k] for k in ("pipeline_run", "task", "state")} if data["produced_by"] else None,
                     "exported_from": data["exported_from"]},
            "inputs": [{"field": i["field"], "role": i["role"], "position": i["position"], "dtype": columns.get(i["field_path"], {}).get("dtype"),
                        "lineage": "not_claimed"}
                       for i in db.execute("SELECT * FROM build_inputs WHERE build_id = %s ORDER BY position NULLS LAST, role, field", (row["id"],))],
            "missing": data["missing"],
        }
    inputs = []
    for i in db.execute("SELECT * FROM build_inputs WHERE build_id = %s ORDER BY position NULLS LAST, role, field", (row["id"],)):
        column = columns.get(i["field_path"], {})
        inputs.append({
            "field": i["field"], "role": i["role"], "position": i["position"], "column": column.get("column"),
            "produced_by": {k: data["produced_by"][k] for k in ("dataform_invocation", "target", "state")},
            "executed_as": {k: data["executed_as"][k] for k in ("project", "location", "job_id", "statement_type", "state")} if data["executed_as"] else None,
            "defined_in": data["defined_in"], "expression": column.get("expression"),
            "derived_from": [d["column"] for d in column.get("derived_from", [])], "lineage": column.get("lineage", "unresolved")})
    return {
        "model": manifest["model"], "version": row["version"], "produced_by": produced, "artifact": artifact,
        "read": {"dataset_version": data["reference"], "snapshot": data["snapshot"], "snapshot_of": data["snapshot_of"], "snapshot_time": data["snapshot_time"]},
        "inputs": inputs,
        "missing": data["missing"],
    }
