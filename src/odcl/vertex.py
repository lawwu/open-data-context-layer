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
                             fields="items(name,bucket,generation,size,crc32c,md5Hash,timeCreated),nextPageToken",
                             **({"pageToken": token} if token else {})).json()
            found += page.get("items") or []
            token = page.get("nextPageToken")
            if not token:
                return found

    def job(self, project: str, location: str, job_id: str) -> dict:
        """One BigQuery job, with its SQL and statistics (jobs.get)."""
        return self._get(f"https://bigquery.googleapis.com/bigquery/v2/projects/{quote(project, safe='')}/jobs/{quote(job_id, safe='')}",
                         location=location).json()

    def jobs(self, project: str, min_ms: int, max_ms: int) -> list[dict]:
        """Every user's finished jobs in `project` created between two times, with their SQL (jobs.list)."""
        found, token = [], None
        while True:
            page = self._get(f"https://bigquery.googleapis.com/bigquery/v2/projects/{quote(project, safe='')}/jobs", allUsers="true", projection="full",
                             stateFilter="done", minCreationTime=str(min_ms), maxCreationTime=str(max_ms), maxResults="200",
                             **({"pageToken": token} if token else {})).json()
            found += page.get("jobs") or []
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

    trained_at = _time(task.get("endTime"))
    relied = [_pin_document(catalog, item, trained_at) for item in manifest.get("relied_on") or []]  # refused before anything is written

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
        _record_relied_on(catalog, build, relied)
        _record_use(catalog, build)
    return {"model": model, "version": version, "change": "added", "inputs": len(manifest["inputs"]), "artifact_verified": verified}


def _pin_document(catalog: Catalog, item, trained_at: datetime | None) -> dict:
    """Which revision of a document a model version relied on. The manifest may name the revision, or the page's
    content digest (the SHA-256 the docs import keeps); else it is the revision current when training ended, or the
    current one when the page was registered only afterwards. Says which it was in `pinned_by`."""
    item = {"document": item} if isinstance(item, str) else item
    require(isinstance(item, dict) and item.get("document"), "relied_on", "relied_on is a list of documents, each a key or {document, revision, digest, why}")
    key = catalog.resolve(str(item["document"]), ("document",))
    why = str(item["why"]).strip()[:500] if item.get("why") else None
    db = catalog.db
    if item.get("revision"):
        found = db.execute("SELECT id FROM revisions WHERE entry_key = %s AND id = %s", (key, item["revision"])).fetchone()
        require(found, "relied_on", f"{key} has no revision {item['revision']}")
        return {"key": key, "revision": found["id"], "pinned_by": "revision", "why": why}
    if item.get("digest"):
        found = db.execute("SELECT id FROM revisions WHERE entry_key = %s AND body ->> 'digest' = %s ORDER BY recorded_at DESC, seq DESC LIMIT 1",
                           (key, item["digest"])).fetchone()
        require(found, "relied_on", f"No revision of {key} has the content digest {item['digest']}: import the page as it was read")
        return {"key": key, "revision": found["id"], "pinned_by": "digest", "why": why}
    if trained_at:
        found = db.execute("SELECT id FROM revisions WHERE entry_key = %s AND recorded_at <= %s ORDER BY recorded_at DESC, seq DESC LIMIT 1",
                           (key, trained_at)).fetchone()
        if found:
            return {"key": key, "revision": found["id"], "pinned_by": "at_training", "why": why}
    return {"key": key, "revision": catalog._head(key)["id"], "pinned_by": "current", "why": why}


def _record_relied_on(catalog: Catalog, build: int, pins: list[dict]) -> None:
    """Pin documents to a model version. A document already pinned to it keeps its pin; only `why` can be added."""
    with catalog.db.cursor() as cursor:
        cursor.executemany("""INSERT INTO build_documents (build_id, document_key, document_revision, pinned_by, why, recorded_by)
                              VALUES (%s, %s, %s, %s, %s, %s)
                              ON CONFLICT (build_id, document_key) DO UPDATE SET why = COALESCE(build_documents.why, excluded.why)""",
                           [(build, p["key"], p["revision"], p["pinned_by"], p["why"], catalog.actor) for p in pins])


def rely(catalog: Catalog, name: str, version: str, documents: list[str], why: str | None = None) -> dict:
    """Record that a model version already imported relied on these documents, each pinned at the revision current
    when its training task ended."""
    require(documents, "relied_on", "Name at least one document")
    row = catalog.db.execute("""SELECT b.id, t.ended_at FROM model_builds b JOIN executions t ON t.id = b.producing_execution
                                WHERE b.project_key = %s AND b.version = %s""", (f"project:{name.strip().lower()}", str(version))).fetchone()
    require(row, "not_found", f"No build of {name} {version} is recorded")
    pins = [_pin_document(catalog, {"document": d, "why": why}, row["ended_at"]) for d in documents]
    with catalog.db.transaction():
        _record_relied_on(catalog, row["id"], pins)
    return {"model": name, "version": str(version), "relied_on": relied_on(catalog, row["id"])}


def relied_on(catalog: Catalog, build: int) -> list[dict]:
    """The documents a model version relied on, each at its pinned revision, and whether the page changed since."""
    return [{"document": r["document_key"], "title": r["title"], "revision": r["document_revision"], "pinned_by": r["pinned_by"], "why": r["why"],
             "changed_since": r["document_revision"] != r["head_revision"]}
            for r in catalog.db.execute("""SELECT d.*, e.head_revision, h.body ->> 'title' AS title FROM build_documents d
                                           JOIN entries e ON e.key = d.document_key JOIN revisions h ON h.id = e.head_revision
                                           WHERE d.build_id = %s ORDER BY d.document_key""", (build,))]


def task_detail(job: dict, pipeline_run: str, name: str) -> dict:
    task = next((t for t in (job.get("jobDetail") or {}).get("taskDetails") or [] if t.get("taskName") == name), None)
    require(task, "task_not_found", f"{pipeline_run} has no task named {name}")
    return task


def run_window(job: dict, pipeline_run: str, task: str | None = None) -> tuple[datetime, datetime | None]:
    """When a pipeline run, or one of its tasks, ran: (start, end). End is None while it is still running."""
    found = task_detail(job, pipeline_run, task) if task else job
    start = _time(found.get("startTime") or found.get("createTime"))
    require(start, "pipeline_run", f"{task or pipeline_run} reports no start time yet")
    return start, _time(found.get("endTime"))


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
    # a bound file set's fields pin no column of their own; the source columns the export job computed them from do
    uses += [(r["source_key"], r["source_revision"], f"{r['role']} in {row['version']}") for r in db.execute("""
        SELECT i.role, s.source_key, s.source_revision FROM build_inputs i
        JOIN version_column_sources s ON (s.dataset_version_id, s.field_path) = (i.dataset_version_id, i.field_path)
        WHERE i.build_id = %s ORDER BY s.source_key, i.role""", (build,))]
    files = db.execute("""SELECT d.table_key, d.table_revision FROM model_builds b JOIN dataset_versions d ON d.id = b.dataset_version_id
                          WHERE b.id = %s AND d.kind = 'gcs_files' AND d.table_key IS NOT NULL""", (build,)).fetchone()
    if files:  # a file set's fields pin no column; the table it was exported from is what the model depends on
        uses.append((files["table_key"], files["table_revision"], f"read as files in {row['version']}"))
    catalog._use_from_model(row["project_key"], uses)


def _file_input(i: dict, column: dict, bound: bool) -> dict:
    """A model input read from a file set: its Parquet type, and when the files are bound to their export job, the
    expression and source columns the job's SELECT gives it."""
    out = {"field": i["field"], "role": i["role"], "position": i["position"], "dtype": column.get("dtype"), "lineage": column.get("lineage", "not_claimed")}
    if bound and "derived_from" in column:
        out |= {"expression": column.get("expression"), "derived_from": [d["column"] for d in column["derived_from"]]}
    return out


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
                     "exported_from": data["exported_from"],
                     **({"exported_by": data["exported_by"], "row_filter": data["row_filter"]} if data["exported_by"] else {})},
            "inputs": [_file_input(i, columns.get(i["field_path"], {}), bool(data["exported_by"]))
                       for i in db.execute("SELECT * FROM build_inputs WHERE build_id = %s ORDER BY position NULLS LAST, role, field", (row["id"],))],
            "relied_on": relied_on(catalog, row["id"]),
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
        "relied_on": relied_on(catalog, row["id"]),
        "missing": data["missing"],
    }
