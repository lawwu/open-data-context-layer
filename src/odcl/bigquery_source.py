"""BigQuery reads for when Sounding is not installed: one table's definition, and the query jobs of a time window.

Sounding (`sounding.source`) needs Python 3.14. This module reads the same two free calls, `tables.get` and
`jobs.list`, with google-cloud-bigquery alone, and returns the fields of Sounding's records that odcl keeps, in the
same shape, so `odcl.bigquery` works on any Python odcl supports. When Sounding is installed, odcl uses it instead.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

JOB_HISTORY_DAYS = 180  # BigQuery keeps job history for 180 days


class SoundingError(ValueError):
    """A request refused before it reached Google, with a message safe to show."""


def public_error(error: Exception) -> tuple[str, int]:
    """A message safe to show for an error from Google, without the request's details."""
    from google.api_core.exceptions import BadRequest, Forbidden, GoogleAPIError, NotFound
    from google.auth.exceptions import DefaultCredentialsError, RefreshError, TransportError
    login = "Run `gcloud auth application-default login`"
    if isinstance(error, SoundingError):
        return str(error), 400
    if isinstance(error, DefaultCredentialsError):
        return f"No Google credentials found. {login}, or set GOOGLE_APPLICATION_CREDENTIALS.", 401
    if isinstance(error, RefreshError):
        return f"Your Google credentials have expired or were revoked. {login} again.", 401
    if isinstance(error, TransportError):
        return "Could not reach Google. Check your network connection and retry.", 502
    if isinstance(error, Forbidden):
        return "BigQuery denied access. Check your Google credentials and permissions.", 403
    if isinstance(error, NotFound):
        return "BigQuery could not find the requested resource.", 404
    if isinstance(error, BadRequest):
        return "BigQuery rejected the request. Check the SQL or table identifier.", 400
    if isinstance(error, GoogleAPIError):
        return "BigQuery is unavailable. Try again later.", 502
    return "The operation failed.", 500


def _int(value) -> int | None:
    return None if value is None else int(value)  # REST int64 fields arrive as strings; absent is unknown, never 0


def _iso(ms) -> str | None:
    return None if ms is None else datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat()


def _ref(r: dict | None, key: str = "tableId") -> str | None:
    return f"{r['projectId']}.{r['datasetId']}.{r[key]}" if r else None


def _flatten(fields: list[dict], prefix: str = ""):
    """(path, field) for every schema field, RECORD parents and their children by dotted path."""
    for f in fields:
        path = f"{prefix}{f['name']}"
        yield path, f
        if f.get("type") in ("RECORD", "STRUCT"):
            yield from _flatten(f.get("fields", []), path + ".")


def _partitioning(r: dict) -> str | None:
    if r.get("timePartitioning"):
        tp = r["timePartitioning"]
        return f"{tp['type']}:{tp.get('field') or '_PARTITIONTIME'}"
    if r.get("rangePartitioning"):
        return f"RANGE:{r['rangePartitioning']['field']}"
    return None


def table(fqn: str, *, client) -> dict:
    """One table, view or snapshot as `tables.get` describes it now: {"table": {...}, "columns": [{...}, ...]}."""
    from google.cloud import bigquery
    if not isinstance(fqn, str) or not fqn.strip():
        raise SoundingError("Table must be project.dataset.table.")
    try:
        ref = bigquery.TableReference.from_string(fqn)
    except ValueError as error:
        raise SoundingError("Table must be project.dataset.table.") from error
    r = client.get_table(ref).to_api_repr()
    name = _ref(r["tableReference"])
    copy = r.get("snapshotDefinition") or r.get("cloneDefinition")
    found = {"fqn": name, "type": r.get("type"), "description": r.get("description"), "location": r.get("location"),
             "partitioning": _partitioning(r), "clustering": ",".join(r["clustering"]["fields"]) if r.get("clustering") else None,
             "base_table": _ref((copy or {}).get("baseTableReference")),
             "base_kind": ("SNAPSHOT" if r.get("snapshotDefinition") else "CLONE") if copy else None,
             "resource": {k: v for k, v in r.items() if k not in ("view", "etag", "selfLink", "id", "kind")}}
    columns = [{"fqn": name, "path": path, "type": f.get("type"), "mode": f.get("mode", "NULLABLE"), "description": f.get("description")}
               for path, f in _flatten((r.get("schema") or {}).get("fields", []))]
    return {"table": found, "columns": columns}


def job_record(job, *, include_sql: bool = False) -> dict:
    """One query job from `jobs.list` as a flat record."""
    props = job._properties
    stats, status = props.get("statistics") or {}, props.get("status") or {}
    query = stats.get("query") or {}
    config = (props.get("configuration") or {}).get("query") or {}
    destination = config.get("destinationTable")
    record = {
        "project": job.project, "location": job.location, "job_id": job.job_id,
        "parent_job_id": stats.get("parentJobId"), "child_jobs": _int(stats.get("numChildJobs")) or 0,
        "created": _iso(stats.get("creationTime")), "started": _iso(stats.get("startTime")), "ended": _iso(stats.get("endTime")),
        "state": status.get("state"), "statement_type": query.get("statementType"),
        "error_reason": (status.get("errorResult") or {}).get("reason"), "cache_hit": query.get("cacheHit"),
        "processed_bytes": _int(query.get("totalBytesProcessed")), "billed_bytes": _int(query.get("totalBytesBilled")),
        "slot_ms": _int(query.get("totalSlotMs", stats.get("totalSlotMs"))),
        "referenced_tables": sorted({_ref(r) for r in (query.get("referencedTables") or []) + (query.get("referencedLogicalViews") or [])}),
        "destination_table": _ref(destination),
        "destination_temporary": destination["datasetId"].startswith("_") if destination else None,  # query results land in a hidden dataset
        "labels": (props.get("configuration") or {}).get("labels") or {},
    }
    return {**record, "sql": config.get("query")} if include_sql else record


def job_pages(project: str, since: datetime, until: datetime, *, client, all_users: bool = True, include_sql: bool = False, page_size: int = 1000):
    """Every query job a project created in [since, until], a page at a time: {"jobs": [...], "unreachable": [...]}.
    A script is followed, in the same page, by its child jobs. A failure part-way raises."""
    if not isinstance(project, str) or not project.strip():
        raise SoundingError("project must be a nonempty string.")
    for name, when in (("since", since), ("until", until)):
        if not isinstance(when, datetime) or when.tzinfo is None:
            raise SoundingError(f"{name} must be a datetime with a time zone.")
    if not since < until:
        raise SoundingError("since must be before until.")
    if until - since > timedelta(days=JOB_HISTORY_DAYS):
        raise SoundingError(f"A window is at most {JOB_HISTORY_DAYS} days, the job history BigQuery keeps.")
    listing = {"project": project, "page_size": page_size, "timeout": 30}
    for page in client.list_jobs(**listing, all_users=all_users, min_creation_time=since, max_creation_time=until).pages:
        jobs, other = [], 0
        for job in page:
            if job.job_type != "query":
                other += 1
                continue
            record = job_record(job, include_sql=include_sql)
            jobs.append(record)
            if record["child_jobs"]:  # BigQuery rejects all_users together with a parent job; a script's children list without it
                for children in client.list_jobs(**listing, parent_job=job.job_id).pages:
                    jobs.extend(job_record(child, include_sql=include_sql) for child in children if child.job_type == "query")
        yield {"jobs": jobs, "other_jobs": other, "unreachable": sorted((page._raw_page or {}).get("unreachable", []))}
