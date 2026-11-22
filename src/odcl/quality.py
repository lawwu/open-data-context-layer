"""Data-quality results: the outcome of a test run against a table or column, pinned to the revision of the entry
that was current when the test ran.

odcl runs no tests. It keeps what dbt (`run_results.json`), or anything that can write a line of JSON per result
(Great Expectations, Dataform assertions, Soda, a notebook), says happened:

    {"entry": "core.sales.daily_totals.amount", "test": "amount_not_negative", "status": "fail", "failures": 12,
     "observed_at": "2026-11-20T06:00:00Z", "run": "nightly-2026-11-20", "kind": "expression", "message": "12 rows below 0"}

A status is pass, warn, fail, error or skip. Results are kept once per source, run, test and entry, so importing a
file again changes nothing. An entry that is not registered is named in the result, not refused.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .store import Catalog, CatalogError, require, stamp

STATUSES = ("pass", "warn", "fail", "error", "skip")
BAD = ("fail", "error")
DBT_STATUS = {"pass": "pass", "success": "pass", "warn": "warn", "fail": "fail", "error": "error", "skipped": "skip", "runtime error": "error"}


def _when(value, where: str) -> datetime:
    if isinstance(value, datetime):
        when = value
    else:
        try:
            when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            raise CatalogError("quality", f"{where}: observed_at {value!r} is not a time") from None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def record(catalog: Catalog, results: list[dict], source: str) -> dict:
    """Keep test results from one source, all or none. Each names an entry, a test, a status and when it ran."""
    catalog.allow("importer")
    require(isinstance(source, str) and source.strip(), "source", "Name where the results come from, such as dbt")
    db, added, kept, unresolved = catalog.db, 0, 0, []
    rows = []
    for n, r in enumerate(results, 1):
        where = f"result {n}"
        require(isinstance(r, dict), "quality", f"{where} is not an object")
        require(isinstance(r.get("test"), str) and r["test"].strip(), "quality", f"{where}: name its test")
        status = str(r.get("status") or "").lower()
        require(status in STATUSES, "quality", f"{where}: status is one of {', '.join(STATUSES)}")
        failures = r.get("failures")
        require(failures is None or (isinstance(failures, int) and failures >= 0), "quality", f"{where}: failures is a count")
        when = _when(r.get("observed_at"), where)
        try:
            key = catalog.resolve(str(r.get("entry") or ""), ("table", "column"))
        except CatalogError:
            unresolved.append(str(r.get("entry")))
            continue
        rows.append((key, r["test"].strip()[:300], (r.get("kind") or None), status, failures, (str(r["message"])[:2000] if r.get("message") else None),
                     when, str(r.get("run") or when.isoformat())[:300]))
    with db.transaction():
        for key, test, kind, status, failures, message, when, run in rows:
            pinned = catalog.history.revision_at(key, when) or catalog.history.head(key)
            inserted = db.execute("""INSERT INTO quality_results (entry_key, entry_revision, source, run, test, test_kind, status, failures, message,
                                                                  observed_at, recorded_by)
                                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (source, run, test, entry_key) DO NOTHING
                                     RETURNING id""",
                                  (key, pinned, source, run, test, kind and str(kind)[:100], status, failures, message, when, catalog.actor)).fetchone()
            added, kept = added + bool(inserted), kept + (not inserted)
    return {"added": added, "unchanged": kept, "unresolved": sorted(set(unresolved))}


def read_jsonl(path: str) -> list[dict]:
    file = Path(path).expanduser()
    require(file.is_file(), "quality", f"{path} is not a file")
    out = []
    for n, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise CatalogError("quality", f"{file.name} line {n} is not JSON: {e.msg}") from None
    require(out, "quality", f"{path} has no results")
    return out


def dbt_results(path: str) -> list[dict]:
    """Test results from a dbt target folder: run_results.json for outcomes, manifest.json for what each test tests."""
    from .dbt import relation_path
    folder = Path(path).expanduser()
    folder = folder.parent if folder.is_file() else folder
    require((folder / "run_results.json").is_file(), "quality", f"No run_results.json in {folder}. Run `dbt test` (or `dbt build`) first")
    require((folder / "manifest.json").is_file(), "quality", f"No manifest.json in {folder}")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    run = json.loads((folder / "run_results.json").read_text(encoding="utf-8"))
    nodes = {**manifest.get("nodes", {}), **manifest.get("sources", {})}
    generated = (run.get("metadata") or {}).get("generated_at")
    out = []
    for r in run.get("results") or []:
        test = nodes.get(r.get("unique_id") or "")
        if not test or test.get("resource_type") != "test":
            continue
        tested = test.get("attached_node") or next((n for n in (test.get("depends_on") or {}).get("nodes", []) if n in nodes), None)
        if tested not in nodes:
            continue
        entry = relation_path(nodes[tested]) + (f".{test['column_name'].lower()}" if test.get("column_name") else "")
        ended = next((t.get("completed_at") for t in r.get("timing") or [] if t.get("name") == "execute"), None) or generated
        out.append({"entry": entry, "test": test.get("name") or r["unique_id"], "kind": (test.get("test_metadata") or {}).get("name") or "singular",
                    "status": DBT_STATUS.get(str(r.get("status")).lower(), "error"), "failures": r.get("failures") if isinstance(r.get("failures"), int) else None,
                    "message": r.get("message"), "observed_at": ended, "run": (run.get("metadata") or {}).get("invocation_id") or generated})
    return out


def _view(r: dict) -> dict:
    return {"entry": r["entry_key"], "test": r["test"], "kind": r["test_kind"], "status": r["status"], "failures": r["failures"], "message": r["message"],
            "observed_at": stamp(r["observed_at"]), "revision": r["entry_revision"], "source": r["source"], "run": r["run"],
            **({"current": r["entry_revision"] == r["head"]} if "head" in r else {})}


def latest(catalog: Catalog, keys: list[str], at: datetime | None = None) -> list[dict]:
    """The newest result of each test on these entries, as of `at` (default now)."""
    rows = catalog.db.execute("""SELECT DISTINCT ON (q.entry_key, q.source, q.test) q.*, e.head_revision AS head
                                 FROM quality_results q JOIN entries e ON e.key = q.entry_key
                                 WHERE q.entry_key = ANY(%s) AND (%s::timestamptz IS NULL OR q.observed_at <= %s)
                                 ORDER BY q.entry_key, q.source, q.test, q.observed_at DESC, q.id DESC""", (list(keys), at, at)).fetchall()
    return [_view(r) for r in rows]


def for_entry(catalog: Catalog, name: str, at=None) -> dict:
    """The newest result of each test on a table (and its columns) or a column, as of `at`."""
    key = catalog.resolve(name, ("table", "column"))
    if isinstance(at, str):
        from .times import moment
        at = moment(at, "at")
    keys = [key, *(r["entry_key"] for r in catalog.history.columns(key))] if key.startswith("table:") else [key]
    found = latest(catalog, keys, at)
    return {"key": key, "at": stamp(at) if at else None, "results": found,
            "failing": [r for r in found if r["status"] in BAD], "status": _status(found)}


def _status(results: list[dict]) -> str | None:
    statuses = {r["status"] for r in results}
    return next((s for s in ("error", "fail", "warn", "pass") if s in statuses), None)
