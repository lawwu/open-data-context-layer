"""How much tables are used, from the query-job history already synced (`odcl sync bigquery-jobs`).

A read is one job that reported reading the table; a script and the statements inside it count once. Popularity
says a table is queried, and how many different query shapes read it; it does not say who asked, which the job
history does not keep. Tables read by the same jobs are "read with" each other, which is how people find what to
join a table to.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from .store import Catalog, CatalogError, now, require, stamp

WINDOW = timedelta(days=30)


def boost(reads: int) -> float:
    """How much a search match is lifted by its table's reads: 4 reads lift it 16%, 100 by 46%. Enough to order
    close matches, not to put a much-read table that barely matches above one that matches well."""
    return 1 + 0.1 * math.log1p(reads)

# One row per top-level job (a script, or a statement outside one) and table it read.
READS = """
    SELECT DISTINCT j.execution_project, coalesce(j.parent_job_id, j.job_id) AS job, lower(t.table_key) AS path, j.created_at, j.pattern_id
    FROM query_tables t JOIN query_jobs j USING (execution_project, location, job_id)
    WHERE t.role = 'read' AND j.created_at >= %(since)s"""


def _since(since) -> datetime:
    since = now() - since if isinstance(since, timedelta) else since
    require(isinstance(since, datetime) and since.tzinfo is not None, "since", "since is a span of time or a moment with a time zone")
    return since


def reads(catalog: Catalog, tables: list[str], since=WINDOW) -> dict[str, dict]:
    """For each table key: how many jobs read it since `since`, through how many query shapes, and when last."""
    paths = {t.removeprefix("table:").lower(): t for t in tables}
    out = {t: {"reads": 0, "shapes": 0, "last_read": None} for t in tables}
    if not paths:
        return out
    for r in catalog.db.execute(f"""
            SELECT path, count(DISTINCT (execution_project, job)) AS reads, count(DISTINCT pattern_id) AS shapes, max(created_at) AS last
            FROM ({READS}) r WHERE path = ANY(%(paths)s) GROUP BY path""", {"since": _since(since), "paths": list(paths)}):
        out[paths[r["path"]]] = {"reads": r["reads"], "shapes": r["shapes"], "last_read": stamp(r["last"])}
    return out


def read_with(catalog: Catalog, table: str, since=WINDOW, limit: int = 10) -> list[dict]:
    """The tables most often read by the same jobs as this one, with how many jobs read both."""
    try:
        path = catalog.resolve(table, ("table",))[6:]
    except CatalogError:  # a table the job history knows and the catalog does not
        path = table.strip().removeprefix("table:")
    rows = catalog.db.execute(f"""
        WITH r AS ({READS})
        SELECT o.path, count(DISTINCT (o.execution_project, o.job)) AS together
        FROM r mine JOIN r o ON o.execution_project = mine.execution_project AND o.job = mine.job AND o.path <> mine.path
        WHERE mine.path = %(path)s GROUP BY o.path ORDER BY together DESC, o.path LIMIT %(limit)s""",
                              {"since": _since(since), "path": path.lower(), "limit": limit}).fetchall()
    known = catalog.history.known(f"table:{r['path']}" for r in rows)
    return [{"table": r["path"], "key": f"table:{r['path']}" if f"table:{r['path']}" in known else None, "jobs": r["together"]} for r in rows]


def popular(catalog: Catalog, since=WINDOW, limit: int = 50) -> list[dict]:
    """The tables read by the most jobs since `since`, registered or not."""
    require(isinstance(limit, int) and 1 <= limit <= 1000, "limit", "limit is between 1 and 1000")
    rows = catalog.db.execute(f"""
        SELECT path, count(DISTINCT (execution_project, job)) AS reads, count(DISTINCT pattern_id) AS shapes, max(created_at) AS last
        FROM ({READS}) r GROUP BY path ORDER BY reads DESC, path LIMIT %(limit)s""", {"since": _since(since), "limit": limit}).fetchall()
    known = catalog.history.known(f"table:{r['path']}" for r in rows)
    return [{"table": r["path"], "key": f"table:{r['path']}" if f"table:{r['path']}" in known else None, "reads": r["reads"], "shapes": r["shapes"],
             "last_read": stamp(r["last"])} for r in rows]
