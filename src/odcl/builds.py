"""Build handoffs and the traces read from them.

A table name and its latest successful job do not say which run produced the rows a model read. The pilot binds
them on purpose: a successful build hands on a uniquely named snapshot of its output before the next write, and
records the run, the target and the snapshot together. `record_handoff` checks that claim against what the
catalog knows and stores it, with every field of the snapshot pinned to the column definition the run used.
`explain_dataset` reads it back, joining the BigQuery job from synced query history at read time.
"""
from __future__ import annotations

from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from .store import Catalog, require, stamp

ACTION_STATES_THAT_WROTE = ("SUCCEEDED",)


def reference_key(reference: str) -> str:
    """A dataset version reference as stored: the table lower-cased, the time as written."""
    reference = reference.strip()
    if reference.startswith("gs://"):  # object names are case-sensitive
        return reference
    table, at, time = reference.partition("@")
    return table.lower().removeprefix("table:") + at + time


def _zulu(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _action(catalog: Catalog, invocation: str, target: str) -> dict:
    action = catalog.db.execute("SELECT * FROM executions WHERE system = 'dataform' AND kind = 'action' AND native_key = %s",
                                (f"{invocation}/actions/{target}",)).fetchone()
    require(action, "not_found", f"{invocation} has no action for {target}. Import it first: odcl import --dataform {invocation}")
    return action


def later_writes(catalog: Catalog, table: str, after: datetime, before: datetime, project: str | None, producer_job: str | None) -> list[str] | None:
    """Jobs other than the producer's that wrote `table` successfully between the build starting and the snapshot.

    The producer's job is often a script (Dataform runs every action as one), and the statement that writes the
    table is a child job of it; the producer's descendants count as the producer. An empty list means synced
    history for the producer's project reaches the snapshot and shows none. None means no synced history reaches
    it, so another write cannot be ruled out. Writes run from projects that were never synced are not seen."""
    rows = catalog.db.execute("""
        WITH RECURSIVE own (job_id) AS (
            SELECT %(producer)s::text WHERE %(producer)s::text IS NOT NULL
            UNION SELECT c.job_id FROM query_jobs c JOIN own ON c.parent_job_id = own.job_id WHERE c.execution_project = %(project)s)
        SELECT DISTINCT j.execution_project || '/' || j.job_id AS job FROM query_tables t JOIN query_jobs j USING (execution_project, location, job_id)
        WHERE lower(t.table_key) = lower(%(table)s) AND t.role = 'write' AND j.created_at > %(after)s AND j.created_at <= %(before)s
          AND NOT (j.execution_project IS NOT DISTINCT FROM %(project)s AND j.job_id IN (SELECT job_id FROM own)) ORDER BY 1""",
        {"table": table, "after": after, "before": before, "project": project, "producer": producer_job}).fetchall()
    if rows:
        return [r["job"] for r in rows]
    covered = catalog.db.execute("SELECT 1 FROM sync_cursors WHERE source = 'bigquery.jobs' AND execution_project = %s AND completed_until >= %s",
                                 (project, before)).fetchone()
    return [] if covered else None


def record_handoff(catalog: Catalog, invocation: str, target: str, snapshot: dict) -> dict:
    """Bind a snapshot to the Dataform action that built the table it copies.

    `snapshot` is what BigQuery says about the snapshot table: {"table", "kind", "base_table", "snapshot_time",
    "schema": [field, ...]}. The action must have succeeded, the snapshot must copy the action's target, be taken
    after the action ended, and no other write to the target may be known in between. Recording the same handoff
    again changes nothing; claiming the same snapshot for another run is refused."""
    catalog.allow("importer")
    target = target.strip().lower().removeprefix("table:")
    action = _action(catalog, invocation, target)
    require(action["state"] in ACTION_STATES_THAT_WROTE, "not_succeeded",
            f"The action for {target} in {invocation} is {action['state']}, so it produced nothing to hand on")
    require(snapshot.get("kind") == "SNAPSHOT", "not_a_snapshot", f"{snapshot.get('table')} is not a table snapshot")
    require((snapshot.get("base_table") or "").lower() == target, "snapshot_of_other_table",
            f"{snapshot['table']} copies {snapshot.get('base_table')}, not {target}")
    taken = datetime.fromisoformat(snapshot["snapshot_time"].replace("Z", "+00:00"))
    require(action["ended_at"] is not None and taken >= action["ended_at"], "snapshot_before_build",
            f"{snapshot['table']} was taken at {_zulu(taken)}, before the build of {target} ended" + (f" at {_zulu(action['ended_at'])}" if action["ended_at"] else ""))
    others = later_writes(catalog, target, action["started_at"] or action["ended_at"], taken, action["job_project"], action["job_id"])
    require(not others, "written_again", f"{target} was written again before the snapshot, by {', '.join(others or [])}; the snapshot may not hold this build's rows")
    reference = f"{snapshot['table'].lower()}@{_zulu(taken)}"
    db = catalog.db
    with db.transaction():
        catalog._lock(reference)
        existing = db.execute("SELECT producer_execution FROM dataset_versions WHERE reference = %s", (reference,)).fetchone()
        if existing:
            require(existing["producer_execution"] == action["id"], "conflict", f"{reference} is already recorded as the output of another run")
            return {"reference": reference, "change": "unchanged"}
        version = db.execute("""
            INSERT INTO dataset_versions (reference, snapshot_table, snapshot_time, table_key, table_revision, producer_execution, verification, recorded_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (reference, snapshot["table"].lower(), taken, f"table:{target}", action["definition_revision"], action["id"],
             Jsonb({"snapshot_of": target, "snapshot_after_build": True, "later_writes_seen": others is not None, "fields": snapshot.get("schema")}),
             catalog.actor)).fetchone()["id"]
        fields = {f.lower() for f in snapshot.get("schema") or []}
        columns = (action["evidence"].get("columns") or {}).items()
        with db.cursor() as cursor:
            cursor.executemany("INSERT INTO version_columns (dataset_version_id, field_path, column_key, column_revision, lineage_status) VALUES (%s, %s, %s, %s, %s)",
                               [(version, field, c["key"], c["revision"], c["lineage"]) for field, c in sorted(columns) if not fields or field in fields])
    return {"reference": reference, "change": "added", "fields": len(columns)}


def _version(catalog: Catalog, reference: str) -> dict:
    reference = reference.strip()
    if "@" in reference:
        row = catalog.db.execute("SELECT * FROM dataset_versions WHERE reference = %s", (reference_key(reference),)).fetchone()
    elif reference.startswith("gs://"):  # a file set's location: its latest recording
        row = catalog.db.execute("SELECT * FROM dataset_versions WHERE source_uri = %s ORDER BY recorded_at DESC, id DESC LIMIT 1",
                                 (reference.rstrip("/"),)).fetchone()
    else:
        row = catalog.db.execute("SELECT * FROM dataset_versions WHERE lower(snapshot_table) = lower(%s) ORDER BY snapshot_time DESC LIMIT 1",
                                 (reference.removeprefix("table:"),)).fetchone()
    require(row, "not_found", f"No dataset version is recorded for {reference}. Record one with odcl handoff")
    return row


def explain_dataset(catalog: Catalog, reference: str) -> dict:
    """What a dataset version is and where it came from: the run that produced it, the BigQuery job that ran it,
    the file and commit that defined it, and each field's expression and inputs, all as pinned when it was built.

    `reference` is project.dataset.snapshot@time, or just the snapshot table for its latest version. `missing`
    says, in words, every link the catalog could not establish."""
    db, version = catalog.db, _version(catalog, reference)
    if version["kind"] == "gcs_files":
        from .filesets import explain
        return explain(catalog, version)
    action = db.execute("SELECT * FROM executions WHERE id = %s", (version["producer_execution"],)).fetchone()
    invocation = db.execute("SELECT * FROM executions WHERE id = %s", (action["parent_id"],)).fetchone() if action["parent_id"] else None
    evidence, missing = action["evidence"], []
    target = version["table_key"].removeprefix("table:")

    job = None
    if action["job_id"]:
        job = db.execute("SELECT * FROM query_jobs WHERE execution_project = %s AND job_id = %s ORDER BY (location = %s) DESC LIMIT 1",
                         (action["job_project"], action["job_id"], action["job_location"])).fetchone()
        if job is None:
            missing.append(f"job {action['job_id']} is not in the synced query history of {action['job_project']}")
    else:
        missing.append("the action reported no BigQuery job")
    if not evidence.get("commit"):
        source = f"the workspace {evidence['workspace']}" if evidence.get("workspace") else "source that names no resolved commit"
        missing.append(f"the definition was compiled from {source}, with no git commit, so its file cannot be pinned to a version")
    if version["table_revision"] is None:
        missing.append("the definition this build used is not pinned: a newer compilation of the repository was imported first")

    columns = []
    for row in db.execute("SELECT * FROM version_columns WHERE dataset_version_id = %s ORDER BY field_path", (version["id"],)).fetchall():
        body = catalog.history.revision(row["column_revision"])["body"]
        inputs = catalog.history.links(row["column_revision"], "derived_from")
        columns.append({"field": row["field_path"], "column": row["column_key"].removeprefix("column:"), "revision": row["column_revision"],
                        "expression": body.get("definition"), "lineage": row["lineage_status"],
                        "derived_from": [{"column": i["target_key"].removeprefix("column:"), "revision": i["target_revision"],
                                          "changed_since": i["target_revision"] != i["head_revision"]} for i in inputs]})
        if row["lineage_status"] != "resolved":
            missing.append(f"{row['field_path']}: its lineage is only partly traced from the build's SQL")
    if not columns and version["table_revision"] is not None:
        missing.append("no field of this build is known from its SQL")

    others = later_writes(catalog, target, action["started_at"] or action["ended_at"], version["snapshot_time"], action["job_project"], action["job_id"])
    if others is None:
        missing.append(f"no synced query history of {action['job_project']} reaches {_zulu(version['snapshot_time'])}, "
                       "so another write before the snapshot cannot be ruled out")
    return {
        "reference": version["reference"], "snapshot": version["snapshot_table"], "snapshot_of": target, "snapshot_time": _zulu(version["snapshot_time"]),
        "produced_by": {"dataform_invocation": invocation["native_key"] if invocation else None, "target": target, "state": action["state"],
                        "started": stamp(action["started_at"]) if action["started_at"] else None, "ended": stamp(action["ended_at"]) if action["ended_at"] else None,
                        "compilation_result": evidence.get("compilation_result")},
        "executed_as": {"project": job["execution_project"], "location": job["location"], "job_id": job["job_id"], "statement_type": job["statement_type"],
                        "state": job["state"], "error_reason": job["error_reason"], "billed_bytes": job["billed_bytes"]} if job else None,
        "defined_in": {"file": evidence.get("file"), "commit": evidence.get("commit")},
        "definition_revision": version["table_revision"],
        "columns": columns,
        "checks": {"snapshot_copies_target": True, "snapshot_after_build": True,
                   "written_again_before_snapshot": None if others is None else bool(others), **({"written_by": others} if others else {})},
        "missing": missing,
        "recorded_by": version["recorded_by"], "recorded_at": stamp(version["recorded_at"]),
    }
