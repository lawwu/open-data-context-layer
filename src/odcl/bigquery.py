"""BigQuery into the catalog: table schemas as entries, and query-job history as evidence of use.

Every BigQuery call goes through Sounding (`sounding.source`), which uses only free calls: `tables.get` for
schemas and `jobs.list` for jobs. This module decides what the catalog keeps: one row per job however often a
window is read, what each job reported reading and writing, and a query's shape with its literals removed.

A job reading a table is evidence that the table was queried. It says nothing about which fields a model used.
"""
from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import store
from .lineage import MAX_SQL, template  # noqa: F401 - the SQL shape is shared with the OpenLineage importer
from .store import Catalog, CatalogError, require

try:
    from sounding import source
    from sounding.errors import SoundingError, public_error
except ImportError as e:  # pragma: no cover - depends on the installation
    raise CatalogError("bigquery", "The BigQuery importer needs Sounding: install open-data-context-layer[bigquery] on Python 3.14 or later") from e

SOURCE = "bigquery.jobs"
EVERYWHERE = "*"  # jobs.list answers for every location of a project at once
BACKFILL = timedelta(days=7)
OVERLAP = timedelta(hours=6)  # the longest a query job may run, so one started before the last window ended is read again
HISTORY = timedelta(days=180)  # how long BigQuery keeps jobs
MONITORING_LABELS = ("sounding_action", "odcl_action")  # jobs a metadata collector ran are not use of the data
DECORATOR = re.compile(r"[@$].*$")  # table@1788314400000 reads the table as of a time; table$20260901 names one partition


def client(project: str | None = None):
    from google.cloud import bigquery
    return bigquery.Client(project=project)


@contextmanager
def reading():
    """Turn a refusal from Google or Sounding into one the catalog reports, without the request's details."""
    from google.api_core.exceptions import GoogleAPIError
    from google.auth.exceptions import GoogleAuthError
    try:
        yield
    except (SoundingError, GoogleAPIError, GoogleAuthError) as e:
        raise CatalogError("bigquery", public_error(e)[0]) from None


# ---- schemas -------------------------------------------------------------------------------------------------
def table_entries(found: dict) -> list[tuple[str, dict]]:
    """(kind, payload) entries for one table Sounding read: the table, then every field by dotted path.

    Only what defines the table is kept. Row counts, sizes and modification times change with the data and
    would make every import a new revision."""
    table = found["table"]
    snapshot = (table["resource"].get("snapshotDefinition") or {}).get("snapshotTime")
    where = {"fqn": table["fqn"], "location": table.get("location"), "base_table": table.get("base_table"),
             "base_kind": table.get("base_kind"), "snapshot_time": snapshot, "partitioning": table.get("partitioning"),
             "clustering": table.get("clustering")}
    entries = [("table", {"path": table["fqn"], "description": (table.get("description") or "")[:4000] or None, "system": "bigquery",
                          "type": (table.get("type") or "table").lower(), "source": {"bigquery": {k: v for k, v in where.items() if v is not None}}})]
    for column in found["columns"]:
        entries.append(("column", {"table": "table:" + table["fqn"], "name": column["path"], "dtype": column["type"],
                                   "description": (column.get("description") or "")[:4000] or None,
                                   "source": {"bigquery": {"path": column["path"], "mode": column["mode"]}}}))
    return entries


def import_tables(catalog: Catalog, tables: list[str], *, client) -> dict:
    """Register these BigQuery tables and their columns as they are defined now. All of them, or none.

    What other importers said about the same tables, such as how Dataform computes a column, is kept."""
    require(tables, "tables", "Name at least one table as project.dataset.table")
    with reading():
        found = [source.table(fqn, client=client) for fqn in tables]
    return catalog.apply([entry for table in found for entry in table_entries(table)], merge=True)


def snapshot(fqn: str, *, client) -> dict:
    """What BigQuery says about a table snapshot, in the shape `builds.record_handoff` checks."""
    with reading():
        found = source.table(fqn, client=client)
    table = found["table"]
    return {"table": table["fqn"], "kind": table.get("base_kind") or table.get("type"), "base_table": table.get("base_table"),
            "snapshot_time": (table["resource"].get("snapshotDefinition") or {}).get("snapshotTime"),
            "schema": [c["path"] for c in found["columns"] if "." not in c["path"]]}


# ---- query shapes --------------------------------------------------------------------------------------------
def _store_job(db, record: dict, shape: str | None, capture: bool) -> bool:
    """Keep one job. Answers whether it is new. A finished job is never replaced by an unfinished reading of it."""
    gaps = [] if record["referenced_tables"] else ["no_table_references"]
    pattern = None
    if capture and shape is None:
        gaps.append("template_unavailable")
    elif capture:
        pattern = db.execute("INSERT INTO query_patterns (scope, template_digest, template) VALUES (%s, %s, %s) "
                             "ON CONFLICT (scope, template_digest) DO UPDATE SET template = excluded.template RETURNING id",
                             (record["project"], hashlib.sha256(shape.encode()).hexdigest(), shape)).fetchone()["id"]
    kept = db.execute("""
        INSERT INTO query_jobs (execution_project, location, job_id, parent_job_id, created_at, started_at, ended_at, state, statement_type,
                                error_reason, cache_hit, processed_bytes, billed_bytes, slot_ms, pattern_id, gaps)
        VALUES (%(project)s, %(location)s, %(job_id)s, %(parent_job_id)s, %(created)s, %(started)s, %(ended)s, %(state)s, %(statement_type)s,
                %(error_reason)s, %(cache_hit)s, %(processed_bytes)s, %(billed_bytes)s, %(slot_ms)s, %(pattern)s, %(gaps)s)
        ON CONFLICT (execution_project, location, job_id) DO UPDATE SET
          parent_job_id = excluded.parent_job_id, started_at = excluded.started_at, ended_at = excluded.ended_at, state = excluded.state,
          statement_type = excluded.statement_type, error_reason = excluded.error_reason, cache_hit = excluded.cache_hit,
          processed_bytes = excluded.processed_bytes, billed_bytes = excluded.billed_bytes, slot_ms = excluded.slot_ms,
          pattern_id = COALESCE(excluded.pattern_id, query_jobs.pattern_id), gaps = excluded.gaps, observed_at = now()
        WHERE query_jobs.state <> 'DONE' OR excluded.state = 'DONE'
        RETURNING (xmax = 0) AS added""", record | {"pattern": pattern, "gaps": gaps}).fetchone()
    if kept is None:
        return False
    job = (record["project"], record["location"], record["job_id"])
    # a time-travel or partition decorator is how the table was read, not another table
    tables = [(*job, table, "read", "jobs.list referencedTables") for table in sorted({DECORATOR.sub("", t) for t in record["referenced_tables"]})]
    succeeded = record["state"] == "DONE" and not record["error_reason"]
    if succeeded and record["destination_table"] and not record["destination_temporary"]:
        tables.append((*job, DECORATOR.sub("", record["destination_table"]), "write", "jobs.list destinationTable"))
    db.execute("DELETE FROM query_tables WHERE execution_project = %s AND location = %s AND job_id = %s", job)
    with db.cursor() as cursor:
        cursor.executemany("INSERT INTO query_tables (execution_project, location, job_id, table_key, role, evidence) VALUES (%s, %s, %s, %s, %s, %s)", tables)
    return kept["added"]


def sync_jobs(catalog: Catalog, project: str, *, client, until: datetime | None = None, backfill: timedelta = BACKFILL,
              overlap: timedelta = OVERLAP, sql: str = "templates") -> dict:
    """Read the query jobs an execution project created since the last finished window, and keep each once.

    The first read goes back `backfill`. Later reads start `overlap` before the last finished window ended, or
    at the oldest job still unfinished, whichever is earlier. Each page of jobs is committed together. The
    window is recorded as finished only after its last page, so an interrupted read is simply read again.

    `sql` is "templates" (keep each query's shape, without literals) or "none" (do not ask BigQuery for SQL)."""
    require(sql != "full", "sql", "Keeping original SQL text is not offered yet: it needs its own access rules. Use templates or none")
    require(sql in ("templates", "none"), "sql", "sql is templates or none")
    require(isinstance(project, str) and project.strip(), "project", "Name the project the jobs ran in")
    db, until = catalog.db, until or store.now()
    require(until.tzinfo is not None, "until", "until needs a time zone")
    cursor = db.execute("SELECT completed_until FROM sync_cursors WHERE source = %s AND execution_project = %s AND location = %s",
                        (SOURCE, project, EVERYWHERE)).fetchone()
    if cursor:
        unfinished = db.execute("SELECT min(created_at) AS oldest FROM query_jobs WHERE execution_project = %s AND state <> 'DONE'", (project,)).fetchone()["oldest"]
        since = min(cursor["completed_until"] - overlap, unfinished or until)
    else:
        since = until - backfill
    since = max(since, until - HISTORY).astimezone(timezone.utc)
    require(since < until, "until", "until is before the window already read")
    counts = {"jobs": 0, "added": 0, "excluded": 0, "pages": 0}
    unreachable: set[str] = set()
    with reading():
        for page in source.job_pages(project, since, until, client=client, include_sql=sql == "templates"):
            with db.transaction():
                for record in page["jobs"]:
                    if any(label in record["labels"] for label in MONITORING_LABELS):
                        counts["excluded"] += 1
                        continue
                    counts["jobs"] += 1
                    counts["added"] += _store_job(db, record, template(record.get("sql")) if sql == "templates" else None, sql == "templates")
            counts["pages"] += 1
            unreachable.update(page["unreachable"])
    if not unreachable:  # a location Google could not reach was not read, so the window is not finished
        db.execute("INSERT INTO sync_cursors (source, execution_project, location, completed_until) VALUES (%s, %s, %s, %s) "
                   "ON CONFLICT (source, execution_project, location) DO UPDATE SET "
                   "completed_until = GREATEST(sync_cursors.completed_until, excluded.completed_until), updated_at = now()",
                   (SOURCE, project, EVERYWHERE, until))
    return {"project": project, "since": store.stamp(since), "until": store.stamp(until), **counts, "sql": sql,
            "unreachable": sorted(unreachable), "finished": not unreachable}
