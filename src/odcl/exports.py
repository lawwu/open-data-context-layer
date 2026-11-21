"""Binding a file set to the BigQuery EXPORT DATA job that wrote it.

A data-loader task usually writes its files with one statement:

    EXPORT DATA OPTIONS (uri = 'gs://…/output_train_dataset/data/data_*.parquet', format = 'PARQUET') AS
    SELECT …, label AS target_regression, … FROM `p.d.gold_table` WHERE time_split = 'train' AND …

That SELECT is the definition of each file column, so a bound field is traced through it with sqlglot, the same way a
Dataform action is: to the catalog columns it is computed from, pinned to a revision. Matching file fields to table
columns by name would be wrong, because columns are renamed (`label AS target_regression`). The WHERE clause's
columns are the version's row filters: they decide which rows the files hold, not where a value comes from.

The job is accepted only if it is an EXPORT DATA statement that succeeded, wrote under the file set's location and
ran before the files were created. If there are more or fewer files than it wrote, the files were rewritten afterwards
(compacted, say). The binding is then kept only when their row count equals the job's, and the trace says so.

What it does not establish, said under `missing`: the source table's definition as it was when the job ran, unless
the catalog recorded a revision before then; and any step that rewrote the files after the export.

Reads are free: BigQuery `jobs.get` and `jobs.list`.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from . import lineage
from .store import Catalog, CatalogError, require

DIALECT = "bigquery"
JOB = re.compile(r"^(?P<project>.+):(?P<location>[A-Za-z0-9-]+)\.(?P<job>[A-Za-z0-9_-]+)$")


def job_ref(text: str) -> tuple[str, str, str]:
    """(project, location, job id) from PROJECT:LOCATION.JOB_ID, as `bq show -j` names a job."""
    found = JOB.match((text or "").strip())
    require(found, "export_job", f"Name an export job as PROJECT:LOCATION.JOB_ID, not {text!r}")
    return found["project"], found["location"], found["job"]


def name(project: str, location: str, job: str) -> str:
    return f"{project}:{location}.{job}"


def statement(sql: str) -> tuple[str, str]:
    """(the SELECT, the uri option) of an EXPORT DATA statement."""
    import sqlglot
    from sqlglot import exp
    try:
        tree = sqlglot.parse_one(sql or "", dialect=DIALECT)
    except Exception:  # noqa: BLE001 - SQL that cannot be read binds nothing
        tree = None
    require(isinstance(tree, exp.Export) and isinstance(tree.this, exp.Query), "export_job", "The job's SQL is not one EXPORT DATA … AS SELECT statement")
    uri = next((p.args["value"].name for p in (tree.args.get("options") or exp.Properties()).expressions
                if isinstance(p, exp.Property) and p.name.lower() == "uri" and isinstance(p.args.get("value"), exp.Literal)), None)
    require(uri, "export_job", "The EXPORT DATA statement names no uri option")
    return tree.this.sql(dialect=DIALECT), uri


def _ms(value) -> datetime | None:
    return datetime.fromtimestamp(int(value) / 1000, timezone.utc) if value else None


def _time(value) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value


def _under(uri: str, root: str) -> bool:
    return uri.startswith(root.rstrip("/") + "/")


def find(client, pipeline_run: str, task: dict, source_uri: str) -> str:
    """The one EXPORT DATA job, created while `task` ran, that wrote under `source_uri`. Refused if there is none or
    more than one, naming them."""
    project = pipeline_run.split("/")[1]
    start, end = _time(task.get("startTime")), _time(task.get("endTime"))
    require(start and end, "export_job", "The task reports no start and end time to search its export jobs by")
    matches = []
    for job in client.jobs(project, int(start.timestamp() * 1000), int(end.timestamp() * 1000)):
        query = ((job.get("configuration") or {}).get("query") or {}).get("query") or ""
        if ((job.get("statistics") or {}).get("query") or {}).get("statementType") != "EXPORT_DATA":
            continue
        try:
            _, uri = statement(query)
        except CatalogError:
            continue
        if _under(uri, source_uri):
            ref = job["jobReference"]
            matches.append(name(ref["projectId"], ref.get("location") or "US", ref["jobId"]))
    require(matches, "export_job_not_found", f"No EXPORT DATA job writing under {source_uri} was created in {project} while the task ran "
                                             f"({start.isoformat()} to {end.isoformat()}); name it with --export-job")
    require(len(matches) == 1, "export_job_ambiguous", f"Several EXPORT DATA jobs wrote under {source_uri}: {', '.join(sorted(matches))}; "
                                                       "name the one or ones that wrote these files with --export-job")
    return matches[0]


def _pin(db, key: str, before: datetime) -> tuple[str, str] | None:
    """The revision of `key` recorded most recently before `before`, else the current one; and which it is."""
    row = db.execute("SELECT id FROM revisions WHERE entry_key = %s AND recorded_at <= %s ORDER BY recorded_at DESC, seq DESC LIMIT 1",
                     (key, before)).fetchone()
    if row:
        return row["id"], "before_export"
    head = db.execute("SELECT head_revision FROM entries WHERE key = %s", (key,)).fetchone()
    return (head["head_revision"], "at_binding") if head else None


def _schema(catalog: Catalog, tables: list[str]) -> dict:
    """The catalog's columns of each source table, nested as sqlglot reads a schema."""
    schema: dict = {}
    for table in tables:
        columns = [k.removeprefix(f"column:{table}.") for k in catalog._columns(f"table:{table}")]
        if not columns:
            continue
        level = schema
        *path, last = table.split(".")
        for part in path:
            level = level.setdefault(part, {})
        level[last] = {c: "unknown" for c in columns}
    return schema


def _filters(select: str, schema: dict, tables: list[str]) -> tuple[list[tuple[str, str]], str | None]:
    """The (table, column) pairs the outermost WHERE filters on, and the WHERE as written."""
    from sqlglot import exp
    from sqlglot.optimizer.qualify import qualify
    tree = lineage.parse(select, DIALECT)
    if tree is None or not tree.args.get("where"):
        return [], None
    written = lineage.parse(select, DIALECT, as_keyed=False).args["where"].this.sql(dialect=DIALECT)
    try:
        tree = qualify(tree, schema=schema or None, dialect=DIALECT, validate_qualify_columns=False, quote_identifiers=False)
    except Exception:  # noqa: BLE001 - unqualified columns are matched to a lone source table below
        pass
    aliases = {t.alias_or_name.lower(): lineage.relation(t) for t in tree.find_all(exp.Table)}
    pairs = set()
    for column in tree.args["where"].find_all(exp.Column):
        table = aliases.get(column.table.lower()) if column.table else (tables[0] if len(tables) == 1 else None)
        if table:
            pairs.add((table, column.name.lower()))
    return sorted(pairs), written


def evaluate(catalog: Catalog, client, refs: list[str], source_uri: str, objects: list[dict], fields: list[str], sql: str = "templates") -> dict:
    """Check the export jobs against the file set and trace each field through their SELECT. Nothing is written:
    the result is passed to `store`."""
    require(sql in ("templates", "none"), "sql", "sql is templates (keep the job's SQL as its shape) or none")
    db, jobs, traced, filters = catalog.db, [], {}, {}
    first_created = min((_time(o["created"]) for o in objects if o.get("created")), default=None)
    for ref in dict.fromkeys(refs):
        project, location, job_id = job_ref(ref)
        job = client.job(project, location, job_id)
        stats, status = job.get("statistics") or {}, job.get("status") or {}
        kind = (stats.get("query") or {}).get("statementType")
        require(kind == "EXPORT_DATA", "export_job", f"{ref} is a {kind or 'non-query'} job, not EXPORT DATA")
        require(status.get("state") == "DONE" and not status.get("errorResult"), "export_job_failed",
                f"{ref} did not succeed ({status.get('state')}{': ' + status['errorResult'].get('reason', '') if status.get('errorResult') else ''}), "
                "so it wrote no files to bind")
        query = ((job.get("configuration") or {}).get("query") or {}).get("query") or ""
        select, uri = statement(query)
        require(_under(uri, source_uri), "export_job_elsewhere", f"{ref} wrote to {uri}, which is not under {source_uri}")
        created = _ms(stats.get("creationTime"))
        require(created and (first_created is None or created < first_created), "export_after_files",
                f"{ref} was created at {created.isoformat() if created else '?'}, after the files ({first_created.isoformat()}); it cannot have written them")
        export = (stats.get("query") or {}).get("exportDataStatistics") or {}
        sources = lineage.tables(select, DIALECT)
        absent = [t for t in sources if not catalog._head(f"table:{t}")]
        require(not absent, "missing_dependency", f"{ref} reads {', '.join(absent)}, which the catalog does not have. "
                                                  "Register it (odcl import --bigquery …), then bind again")
        schema = _schema(catalog, sources)
        outputs = set(lineage.selected(select, DIALECT, schema))
        for field in fields:
            if field not in outputs:
                continue
            try:
                expression, parents, complete = lineage.column_parents(field, select, DIALECT, schema)
            except Exception:  # noqa: BLE001 - a field sqlglot cannot trace is kept as partial, with no sources
                expression, parents, complete = lineage.as_written(field, select, DIALECT), [], False
            pins = {}
            for table, column in parents:
                pin = _pin(db, f"column:{table}.{column}", created)
                if pin:
                    pins[f"column:{table}.{column}"] = pin
                else:
                    complete = False  # a source column the catalog does not have cannot be pinned
            seen = traced.get(field)
            if seen is None:
                traced[field] = {"expression": expression, "sources": pins, "complete": complete}
            elif (seen["expression"], set(seen["sources"])) != (expression, set(pins)):  # the jobs disagree: not one definition
                seen.update(sources={**seen["sources"], **pins}, complete=False)
        pairs, written = _filters(select, schema, sources)
        for table, column in pairs:
            pin = _pin(db, f"column:{table}.{column}", created)
            if pin:
                filters[f"column:{table}.{column}"] = pin
        shape = lineage.template(written and f"SELECT 1 FROM t WHERE {written}", DIALECT) if sql == "templates" else None
        jobs.append({"project": project, "location": location, "job_id": job_id, "created": created, "ended": _ms(stats.get("endTime")),
                     "uri": uri, "file_count": int(export["fileCount"]) if export.get("fileCount") else None,
                     "row_count": int(export["rowCount"]) if export.get("rowCount") else None,
                     "sql_sha256": hashlib.sha256(query.encode()).hexdigest(), "sql_template": lineage.template(query, DIALECT) if sql == "templates" else None,
                     "row_filter": shape.split(" WHERE ", 1)[1] if shape and " WHERE " in shape else None})

    # are the files the jobs' own output? Only the Parquet objects count: a sidecar such as metadata.json is not the job's
    data = [o for o in objects if o.get("parquet")]
    others = sorted(o["uri"] for o in objects if not o.get("parquet"))
    files = len(data)
    exported_files = sum(j["file_count"] or 0 for j in jobs) if all(j["file_count"] is not None for j in jobs) else None
    exported_rows = sum(j["row_count"] or 0 for j in jobs) if all(j["row_count"] is not None for j in jobs) else None
    footer_rows = sum(o["rows"] for o in data) if data and all(o.get("rows") is not None for o in data) else None
    refs_named = [name(j["project"], j["location"], j["job_id"]) for j in jobs]
    if exported_rows is not None and footer_rows is not None and exported_rows != footer_rows:
        raise CatalogError("export_rows_differ", f"{', '.join(refs_named)} exported {exported_rows:,} rows, but the files hold {footer_rows:,}: "
                                                 "they are not the job's output")
    own = exported_files == files
    if not own:
        require(exported_rows is not None and footer_rows is not None, "export_rows_differ",
                f"{', '.join(refs_named)} wrote {exported_files if exported_files is not None else 'an unknown number of'} files and {files} Parquet "
                "object(s) are recorded; the row counts that would show they hold the same rows are not known")
    return {"jobs": jobs, "jobs_named": refs_named, "fields": traced, "filters": filters, "own_output": own,
            "exported_files": exported_files, "files": files, "rows": exported_rows, "not_counted": others}


def store(catalog: Catalog, version: int, binding: dict) -> str:
    """Write a binding `evaluate` checked. Binding the same jobs again changes nothing; other jobs are refused."""
    catalog.allow("importer")
    from psycopg.types.json import Jsonb
    db = catalog.db
    with db.transaction():
        catalog._lock(f"dataset_version:{version}")
        bound = {name(r["job_project"], r["job_location"], r["job_id"]) for r in
                 db.execute("SELECT * FROM dataset_version_exports WHERE dataset_version_id = %s", (version,))}
        if bound:
            require(bound == set(binding["jobs_named"]), "conflict", f"This file set is already bound to {', '.join(sorted(bound))}; a binding is recorded once")
            return "unchanged"
        with db.cursor() as cursor:
            cursor.executemany("""INSERT INTO dataset_version_exports (dataset_version_id, job_project, job_location, job_id, created_at, ended_at, output_uri,
                                  file_count, row_count, sql_sha256, sql_template, row_filter, recorded_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                               [(version, j["project"], j["location"], j["job_id"], j["created"], j["ended"], j["uri"], j["file_count"], j["row_count"],
                                 j["sql_sha256"], j["sql_template"], j["row_filter"], catalog.actor) for j in binding["jobs"]])
            for field, t in binding["fields"].items():
                cursor.execute("UPDATE version_columns SET expression = %s, lineage_status = %s WHERE dataset_version_id = %s AND field_path = %s",
                               ((t["expression"] or "")[:4000] or None, "resolved" if t["complete"] and t["sources"] else "partial", version, field))
            cursor.executemany("INSERT INTO version_column_sources (dataset_version_id, field_path, source_key, source_revision, pinned) VALUES (%s, %s, %s, %s, %s)",
                               [(version, field, key, rev, pinned) for field, t in binding["fields"].items() for key, (rev, pinned) in sorted(t["sources"].items())])
            cursor.executemany("INSERT INTO version_row_filters (dataset_version_id, source_key, source_revision, pinned) VALUES (%s, %s, %s, %s)",
                               [(version, key, rev, pinned) for key, (rev, pinned) in sorted(binding["filters"].items())])
        db.execute("UPDATE dataset_versions SET verification = verification || %s WHERE id = %s",
                   (Jsonb({"export": {"jobs": binding["jobs_named"], "own_output": binding["own_output"], "exported_files": binding["exported_files"],
                                      "files": binding["files"], "rows": binding["rows"], "not_counted": binding["not_counted"]}}), version))
    return "bound"
