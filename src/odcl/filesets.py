"""File sets: a dataset version that is a set of Cloud Storage files a training run read, such as the Parquet a
pipeline's data-loader task exported, for when the training data was never a BigQuery snapshot.

`record` lists the objects under a task's output artifact (or under gs:// URIs given directly) and keeps each with its
generation, which pins the exact bytes. Each field comes from the Parquet footer, read with a range request, so no
file is downloaded. A file set proves which bytes a trainer read. It does not prove which build produced them, so
its fields pin no column definition and claim no lineage. The table it was exported from, when given, is recorded
as table-level lineage only, pinned as it is when the file set is recorded: the table may have changed since the files
were written.

Reads are free: `pipelineJobs.get`, Cloud Storage `objects.list` and range reads of each object's last bytes.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

from . import parquet
from .store import Catalog, require

TAIL = 64 * 1024  # most footers fit; a larger one is read again at its own size


def _uri(found: dict) -> str:
    return f"gs://{found['bucket']}/{found['name']}"


def objects(client, root: str) -> list[dict]:
    """The objects a URI names: the object itself, or every object under it as a folder. A sibling that only shares
    the name's prefix (`out_old/` beside `out`) is not under it, and neither is an empty marker such as `_SUCCESS`."""
    from .vertex import GCS
    bucket, path = GCS.match(root).groups()
    found = []
    for o in client.list(root):
        marker = int(o.get("size") or 0) == 0 and o["name"].rsplit("/", 1)[-1][:1] in ("_", ".")
        if (o["name"] == path or (o["name"].startswith(path.rstrip("/") + "/") and not o["name"].endswith("/"))) and not marker:
            found.append({"uri": _uri(o), "generation": str(o["generation"]), "size": int(o.get("size") or 0),
                          "crc32c": o.get("crc32c"), "md5": o.get("md5Hash"), "created": o.get("timeCreated")})
    return sorted(found, key=lambda o: o["uri"])


def schema(client, found: list[dict]) -> tuple[list[tuple[str, str]], str]:
    """The fields every Parquet object shares, and "read"; or no fields and the reason they could not be read. Each
    object whose Parquet footer was read is marked `parquet`, with its row count as `rows`."""
    schemas = {}
    for o in found:
        if o["size"] < 12:
            continue  # a marker such as _SUCCESS
        tail = client.tail(o["uri"], o["generation"], min(o["size"], TAIL))
        try:
            needed = parquet.footer_size(tail[-8:])
        except ValueError:
            continue  # not Parquet
        if needed > len(tail):
            tail = client.tail(o["uri"], o["generation"], needed)
        try:
            schemas[o["uri"]], o["rows"] = parquet.metadata(tail)
            o["parquet"] = True
        except ValueError as e:
            return [], f"the Parquet footer of {o['uri']} cannot be read: {e}"
    if not schemas:
        return [], f"no Parquet object was found among the {len(found)} object(s), so their fields are not known"
    first, *rest = schemas.items()
    for uri, fields in rest:
        if fields != first[1]:
            return [], f"the Parquet objects do not share one schema: {uri} differs from {first[0]}"
    return first[1], "read"


def record(catalog: Catalog, *, client, pipeline_run: str | None = None, task: str | None = None, output: str | None = None,
           uris: list[str] | None = None, from_table: str | None = None, export_jobs: list[str] | None = None,
           find_export_job: bool = False, sql: str = "templates", created_by: dict | None = None) -> dict:
    """Record the files a task wrote to one of its outputs, or the files under `uris`, as a dataset version.

    The reference is the artifact URI and the pipeline job (`gs://…/output_train_dataset@job`); for URIs given
    directly, the first URI and its newest object's generation. Recording the same files again changes nothing; the
    same reference naming other bytes is refused.

    With `export_jobs` (BigQuery jobs as PROJECT:LOCATION.JOB_ID) or `find_export_job`, the file set is also bound to
    the EXPORT DATA job that wrote it, and each field is traced through the job's SELECT; see `exports`. A file set
    recorded without a binding can be bound by recording it again with one.

    `created_by` says what wrote the files when no pipeline task did, such as a notebook (see `provenance`). It is
    kept as a person said it, and can be added to a file set recorded before."""
    from . import exports
    from .provenance import check
    from .vertex import GCS, SUCCEEDED, record_task, task_detail
    created_by = check(created_by)
    require(bool(pipeline_run) != bool(uris), "files", "Name the files one way: --pipeline-run with --task and --output, or gs:// URIs")
    detail = job = None
    if pipeline_run:
        require(task and output, "files", "A pipeline run's files are named by its --task and that task's --output")
        job = client.get(pipeline_run)
        detail = task_detail(job, pipeline_run, task)
        require(detail.get("state") in SUCCEEDED, "task_not_succeeded", f"Task {task} is {detail.get('state')}, so it wrote no output to read")
        outputs = detail.get("outputs") or {}
        artifacts = (outputs.get(output) or {}).get("artifacts") or []
        require(artifacts and artifacts[0].get("uri"), "output_not_found",
                f"Task {task} has no output {output}; its outputs are {', '.join(sorted(outputs)) or 'none'}")
        roots = [artifacts[0]["uri"].rstrip("/")]
    else:
        roots = [u.strip().rstrip("/") for u in uris]
        require(all(GCS.match(u) for u in roots), "files", "Name files as gs://bucket/path")
    found = sorted({o["uri"]: o for root in roots for o in objects(client, root)}.values(), key=lambda o: o["uri"])
    require(found, "files_not_found", f"No object is stored under {', '.join(roots)}")
    reference = f"{roots[0]}@{pipeline_run.rsplit('/', 1)[1]}" if pipeline_run else f"{roots[0]}@{max(int(o['generation']) for o in found)}"
    fields, said = schema(client, found)
    if find_export_job:
        require(pipeline_run, "export_job", "--find-export-job searches the task's run for its export job, so it needs --pipeline-run")
        export_jobs = [exports.find(client, pipeline_run, detail, roots[0])]
    binding = exports.evaluate(catalog, client, export_jobs, roots[0], found, [name for name, _ in fields], sql) if export_jobs else None
    table = revision = None
    if from_table:
        table = catalog.resolve(from_table, ("table",))
        revision = catalog._head(table)["id"]

    db = catalog.db
    with db.transaction():
        catalog._lock(reference)
        existing = db.execute("SELECT id FROM dataset_versions WHERE reference = %s", (reference,)).fetchone()
        if existing:
            kept = {r["uri"]: r["generation"] for r in db.execute("SELECT uri, generation FROM dataset_version_objects WHERE dataset_version_id = %s",
                                                                 (existing["id"],))}
            now = {o["uri"]: o["generation"] for o in found}
            differs = sorted(set(kept.items()) ^ set(now.items()))
            require(not differs, "conflict", f"{reference} is already recorded with other objects ({differs[0][0] if differs else ''}); "
                                             "a file set is recorded once")
            # a file set recorded before row counts and creation times were kept gets them now, from what was just read
            with db.cursor() as cursor:
                cursor.executemany("UPDATE dataset_version_objects SET num_rows = COALESCE(num_rows, %s), time_created = COALESCE(time_created, %s) "
                                   "WHERE dataset_version_id = %s AND uri = %s",
                                   [(o.get("rows"), o.get("created"), existing["id"], o["uri"]) for o in found if o.get("rows") is not None or o.get("created")])
            change = exports.store(catalog, existing["id"], binding) if binding else "unchanged"
            if created_by and db.execute("UPDATE dataset_versions SET verification = verification || %s WHERE id = %s AND verification -> 'created_by' "
                                         "IS DISTINCT FROM %s RETURNING id", (Jsonb({"created_by": created_by}), existing["id"], Jsonb(created_by))).fetchone():
                change = "changed"
            return {"reference": reference, "change": change, "objects": len(found), "fields": len(fields), "schema": said,
                    **({"exported_by": binding["jobs_named"]} if binding else {})}
        producer = record_task(db, job, detail, pipeline_run, task) if pipeline_run else None
        version = db.execute("""
            INSERT INTO dataset_versions (reference, kind, source_uri, table_key, table_revision, producer_execution, verification, recorded_by)
            VALUES (%s, 'gcs_files', %s, %s, %s, %s, %s, %s) RETURNING id""",
            (reference, roots[0], table, revision, producer,
             Jsonb({"objects": len(found), "schema": said, "producer_unknown": producer is None, "exported_from": table,
                    "table_may_have_changed": table is not None, "roots": roots, **({"created_by": created_by} if created_by else {}),
                    **({"output": output, "artifact": artifacts[0].get("name")} if pipeline_run else {})}),
             catalog.actor)).fetchone()["id"]
        with db.cursor() as cursor:
            cursor.executemany("INSERT INTO dataset_version_objects (dataset_version_id, uri, generation, size, crc32c, md5, time_created, num_rows) "
                               "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                               [(version, o["uri"], o["generation"], o["size"], o["crc32c"], o["md5"], o.get("created"), o.get("rows")) for o in found])
            cursor.executemany("INSERT INTO version_columns (dataset_version_id, field_path, column_key, column_revision, lineage_status, dtype) "
                               "VALUES (%s, %s, NULL, NULL, 'not_claimed', %s)", [(version, name, dtype) for name, dtype in fields])
        if binding:
            exports.store(catalog, version, binding)
    return {"reference": reference, "change": "added", "objects": len(found), "fields": len(fields), "schema": said,
            **({"exported_by": binding["jobs_named"]} if binding else {})}


def explain(catalog: Catalog, version: dict) -> dict:
    """A file set as recorded: its objects, the task that wrote them, the table they were exported from, its fields
    and everything not established."""
    db = catalog.db
    found = [{"uri": r["uri"], "generation": r["generation"], "size": r["size"]}
             for r in db.execute("SELECT * FROM dataset_version_objects WHERE dataset_version_id = %s ORDER BY uri", (version["id"],))]
    task = db.execute("SELECT * FROM executions WHERE id = %s", (version["producer_execution"],)).fetchone() if version["producer_execution"] else None
    exported = None
    if version["table_key"]:
        head = db.execute("SELECT head_revision FROM entries WHERE key = %s", (version["table_key"],)).fetchone()
        exported = {"table": version["table_key"].removeprefix("table:"), "revision": version["table_revision"],
                    "changed_since": bool(head and head["head_revision"] != version["table_revision"])}
    said = version["verification"].get("schema", "read")
    from .exports import name
    jobs = db.execute("SELECT * FROM dataset_version_exports WHERE dataset_version_id = %s ORDER BY created_at, job_id", (version["id"],)).fetchall()
    sources: dict[str, list[dict]] = {}
    for r in db.execute("""SELECT s.*, e.head_revision FROM version_column_sources s JOIN entries e ON e.key = s.source_key
                           WHERE s.dataset_version_id = %s ORDER BY s.field_path, s.source_key""", (version["id"],)):
        sources.setdefault(r["field_path"], []).append({"column": r["source_key"].removeprefix("column:"), "revision": r["source_revision"],
                                                       "pinned": r["pinned"], "changed_since": r["source_revision"] != r["head_revision"]})
    columns = []
    for r in db.execute("SELECT * FROM version_columns WHERE dataset_version_id = %s ORDER BY field_path", (version["id"],)):
        column = {"field": r["field_path"], "dtype": r["dtype"], "lineage": r["lineage_status"]}
        if jobs and r["lineage_status"] != "not_claimed":
            column |= {"expression": r["expression"], "derived_from": sources.get(r["field_path"], [])}
        columns.append(column)
    filters = [r["source_key"].removeprefix("column:") for r in
               db.execute("SELECT source_key FROM version_row_filters WHERE dataset_version_id = %s ORDER BY source_key", (version["id"],))]
    missing = []
    created_by = version["verification"].get("created_by")
    if task is None and created_by:
        missing.append(f"what wrote these files is as {version['recorded_by']} said ({created_by['kind']} "
                       f"{created_by.get('path') or created_by.get('url') or created_by.get('command') or ''}".rstrip() + "), not checked against a run")
    elif task is None:
        missing.append("the producer of these files is not recorded: they were named by URI, not read from a pipeline task's output")
    if exported:
        missing.append(f"the files were exported from {exported['table']}, pinned as that table was defined when the files were recorded; "
                       "the table may have changed between writing the files and recording them")
    elif not jobs:
        missing.append("no table the files were exported from is recorded")
    if said != "read":
        missing.append(f"the fields are not known: {said}")
    if not jobs:
        missing.append("column-level lineage below the file set is not claimed: its fields come from the Parquet schema, not from a definition")
    else:
        named = ", ".join(name(j["job_project"], j["job_location"], j["job_id"]) for j in jobs)
        unpinned = sorted({s["column"].rsplit(".", 1)[0] for found in sources.values() for s in found if s["pinned"] == "at_binding"})
        for table in unpinned:
            missing.append(f"{table}'s definition is pinned as it is now, not as it was when {named} ran: the catalog has no revision of it "
                           "from before then, so the definition may differ")
        export = version["verification"].get("export") or {}
        if export and not export.get("own_output"):
            missing.append(f"the files are not {named}'s own output: it wrote {export.get('exported_files')} file(s) and {export.get('files')} Parquet "
                           f"object(s) are recorded; they were rewritten after the export, row count equal ({export.get('rows'):,})")
        if export.get("not_counted"):
            missing.append(f"{len(export['not_counted'])} recorded object(s) are not Parquet and were not counted against {named}: "
                           + ", ".join(u.rsplit("/", 1)[-1] for u in export["not_counted"]) + "; they are pinned, but nothing says what wrote them")
        for c in columns:
            if c["lineage"] == "not_claimed":
                missing.append(f"{c['field']} is not output by the export job's SELECT, so its lineage is not claimed")
            elif c["lineage"] != "resolved":
                missing.append(f"{c['field']}: its lineage is only partly traced from the export job's SQL")
    from .store import stamp
    return {
        "reference": version["reference"], "kind": "gcs_files", "uri": version["source_uri"], "objects": found,
        "produced_by": {"pipeline_run": task["evidence"].get("pipeline_run"), "task": task["evidence"].get("task"), "state": task["state"],
                        "started": stamp(task["started_at"]) if task["started_at"] else None, "ended": stamp(task["ended_at"]) if task["ended_at"] else None}
        if task else None,
        **({"created_by": created_by} if created_by else {}),
        "exported_from": exported,
        "exported_by": [{"job": name(j["job_project"], j["job_location"], j["job_id"]), "statement_type": "EXPORT_DATA", "rows": j["row_count"],
                         "files": j["file_count"], "created": stamp(j["created_at"]), "ended": stamp(j["ended_at"]) if j["ended_at"] else None}
                        for j in jobs] or None,
        "row_filter": {"expression": " / ".join(dict.fromkeys(j["row_filter"] for j in jobs if j["row_filter"])) or None, "columns": filters}
        if jobs else None,
        "columns": columns,
        "checks": {"generations_pinned": True},
        "missing": missing,
        "recorded_by": version["recorded_by"], "recorded_at": stamp(version["recorded_at"]),
    }
