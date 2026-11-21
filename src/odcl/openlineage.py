"""OpenLineage events into the catalog: the runs a scheduler, Spark, dbt or any other integration reports, and the
lineage they establish.

An event is one observation of a run (`START`, `RUNNING`, `COMPLETE`, `FAIL`, `ABORT`, `OTHER`), or, with no run, a
job's declared lineage (`JobEvent`) or a dataset's schema (`DatasetEvent`). See https://openlineage.io/docs/spec/.

- **Datasets** become table entries, and their schema facet's fields column entries. A warehouse dataset keeps its
  name as its key, so `bigquery` / `project.dataset.table` is the same entry the BigQuery and Dataform importers
  write, and each keeps what the others said. Anything else (files, buckets) gets a key built from its namespace
  and name, which are kept under `source.openlineage`.
- **Lineage** comes only from a run that completed, or from a job event: a failed run did not build anything. An
  output table is derived from the run's inputs. An output column is derived from the input fields its
  `columnLineage` facet marks DIRECT; an INDIRECT field (a filter, a join key) decides which rows count, not the
  value, so it is not lineage. A transformation's description becomes the column's definition.
- **Runs** are recorded as executions (`system = 'openlineage'`). Events for one run may arrive in any order, and
  once a run has finished a late `START` does not reopen it.
- **What is kept.** Each event is stored once, by digest, so a replay changes nothing. SQL is kept only as its
  shape, without literals, as for BigQuery jobs; error messages are dropped.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime

from psycopg.types.json import Jsonb

from .lineage import template
from .store import PATH, Catalog, CatalogError, require

RUN_STATES = ("START", "RUNNING", "COMPLETE", "ABORT", "FAIL", "OTHER")
FINISHED = ("COMPLETE", "ABORT", "FAIL")
# namespaces whose dataset names are already database.schema.table, the way the other importers key tables
WAREHOUSES = {"bigquery": "bigquery", "postgres": "postgres", "postgresql": "postgres", "mysql": "mysql", "snowflake": "snowflake",
              "redshift": "redshift", "trino": "trino", "presto": "presto", "duckdb": "duckdb", "sqlserver": "tsql", "mssql": "tsql",
              "oracle": "oracle", "hive": "hive", "databricks": "databricks", "awsathena": "athena", "clickhouse": "clickhouse",
              "teradata": "teradata", "spanner": None, "sqlite": "sqlite", "db2": None}
LIMIT = 200  # datasets per event


def _text(value, what: str, limit: int = 1000) -> str:
    require(isinstance(value, str) and value.strip() and len(value) <= limit, "openlineage", f"{what} is required text of at most {limit} characters")
    return value.strip()


def _time(value) -> datetime:
    try:
        at = datetime.fromisoformat(_text(value, "eventTime", 64).replace("Z", "+00:00"))
    except ValueError:
        raise CatalogError("openlineage", f"eventTime is an ISO 8601 time with a zone, not {value!r}") from None
    require(at.tzinfo is not None, "openlineage", "eventTime needs a time zone")
    return at


def scheme(namespace: str) -> str:
    return namespace.split("://", 1)[0].split(":", 1)[0].lower()


def table_path(namespace: str, name: str) -> str:
    """The catalog path for an OpenLineage dataset."""
    if scheme(namespace) in WAREHOUSES and PATH.match(name.lower()):
        return name.lower()
    rest = namespace.split("://", 1)[1] if "://" in namespace else namespace.split(":", 1)[-1]
    parts = [p for p in re.split(r"[/:]+", f"{rest}/{name}") if p]
    path = ".".join([scheme(namespace), *parts]).lower()
    path = re.sub(r"\.{2,}", ".", re.sub(r"[^a-z0-9_.\-$]", "_", path)).strip(".")
    require(PATH.match(path), "openlineage", f"The dataset {namespace} {name} has a name too long to register")
    return path


class _Dataset:
    def __init__(self, raw, role: str):
        require(isinstance(raw, dict), "openlineage", f"Each of {role} is an object with a namespace and a name")
        self.namespace, self.name = _text(raw.get("namespace"), f"A dataset's namespace in {role}"), _text(raw.get("name"), f"A dataset's name in {role}")
        self.path = table_path(self.namespace, self.name)
        self.key = f"table:{self.path}"
        facets = raw.get("facets") or {}
        require(isinstance(facets, dict), "openlineage", f"facets of {self.name} is an object")
        self.fields = list(_fields((facets.get("schema") or {}).get("fields") or []))
        self.description = ((facets.get("documentation") or {}).get("description") or None)
        self.lineage = (facets.get("columnLineage") or {}).get("fields") or {}
        require(isinstance(self.lineage, dict), "openlineage", f"columnLineage.fields of {self.name} is an object")


def _fields(fields, prefix=""):
    """(name, type, description) for every field of a schema facet, nested fields by dotted path."""
    require(isinstance(fields, list), "openlineage", "A schema facet's fields is a list")
    for f in fields:
        require(isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"], "openlineage", "Each schema field has a name")
        name = prefix + f["name"].lower()
        yield name, f.get("type"), f.get("description") or None
        if f.get("fields"):
            yield from _fields(f["fields"], name + ".")


def _inputs_of(field: dict) -> list[tuple[str, str, list[dict]]]:
    out = []
    for i in field.get("inputFields") or []:
        require(isinstance(i, dict), "openlineage", "Each columnLineage input field is an object")
        out.append((table_path(_text(i.get("namespace"), "An input field's namespace"), _text(i.get("name"), "An input field's name")),
                    _text(i.get("field"), "An input field's field", 300).lower(), i.get("transformations") or []))
    return out


def _direct(transformations: list[dict], field: dict) -> bool:
    """Whether an input field is where the value comes from. Events from before transformations were reported say
    only `transformationType`, or nothing; those count as direct."""
    if transformations:
        return any((t or {}).get("type", "DIRECT") == "DIRECT" for t in transformations)
    return (field.get("transformationType") or "DIRECT").upper() != "INDIRECT"


def _shape(event: dict, datasets: list[_Dataset]) -> tuple[dict, str | None]:
    """The event as kept: SQL as its shape, no error message. Also the shape on its own."""
    kept = copy.deepcopy(event)
    sql, shape = ((kept.get("job") or {}).get("facets") or {}).get("sql"), None
    if isinstance(sql, dict):
        dialects = {WAREHOUSES.get(scheme(d.namespace)) for d in datasets} - {None}
        try:
            shape = template(sql.get("query"), dialects.pop() if len(dialects) == 1 else None)
        except ImportError:  # sqlglot is an extra; without it no SQL is kept
            shape = None
        sql["query"] = shape
    facets = (kept.get("run") or {}).get("facets") or {}
    if isinstance(facets.get("errorMessage"), dict):
        facets["errorMessage"] = {k: v for k, v in facets["errorMessage"].items() if k in ("_producer", "_schemaURL", "programmingLanguage")}
    return kept, shape


def _entries(inputs: list[_Dataset], outputs: list[_Dataset]) -> list[tuple[str, dict]]:
    """Tables and columns to register, parents before anything derived from them."""
    tables: dict[str, dict] = {}
    columns: dict[tuple[str, str], dict] = {}

    def table(path, namespace=None, name=None, description=None):
        t = tables.setdefault(path, {"path": path, "system": None, "description": None, "source": {}})
        if namespace:
            t["system"] = t["system"] or scheme(namespace)
            t["source"] = {"openlineage": {"namespace": namespace, "name": name}}
        t["description"] = t["description"] or description

    def column(path, name, dtype=None, description=None):
        c = columns.setdefault((path, name), {"table": f"table:{path}", "name": name, "dtype": None, "description": None})
        c["dtype"], c["description"] = c["dtype"] or (str(dtype)[:200] if dtype else None), c["description"] or description

    for d in inputs + outputs:
        table(d.path, d.namespace, d.name, d.description)
        for name, dtype, description in d.fields:
            column(d.path, name, dtype, description)
    derived: dict[tuple[str, str], dict] = {}
    read = {d.path for d in inputs}
    for d in outputs:
        for out_field, spec in d.lineage.items():
            require(isinstance(spec, dict), "openlineage", f"columnLineage for {out_field} is an object")
            parents, said = [], []
            for path, field, transformations in _inputs_of(spec):
                table(path)
                column(path, field)
                read.add(path)
                if _direct(transformations, spec):
                    parents.append(f"column:{path}.{field}")
                said += [t["description"] for t in transformations if isinstance(t, dict) and t.get("description")]
            said += [spec["transformationDescription"]] if spec.get("transformationDescription") else []
            column(d.path, out_field.lower())
            derived[(d.path, out_field.lower())] = {"derived_from": sorted(set(parents)), "definition": "; ".join(dict.fromkeys(said)) or None}
    produced = {d.path for d in outputs}
    sources = sorted(f"table:{p}" for p in read)
    ordered = [("table", {**t, "source": t["source"] or None}) for p, t in sorted(tables.items()) if p not in produced]
    ordered += [("column", {**c, "source": tables[p]["source"] or None}) for (p, _), c in sorted(columns.items()) if p not in produced]
    for p in sorted(produced):
        ordered.append(("table", {**tables[p], "source": tables[p]["source"] or None,
                                  "derived_from": [s for s in sources if s != f"table:{p}"] or None}))
        for (path, name), c in sorted(columns.items()):
            if path == p:
                ordered.append(("column", {**c, **derived.get((path, name), {}), "source": tables[p]["source"] or None}))
    return ordered


def ingest(catalog: Catalog, event: dict) -> dict:
    """Record one OpenLineage event. Everything it says is checked before anything is written, and it is written
    in one transaction. The same event again changes nothing."""
    catalog.allow("importer")
    require(isinstance(event, dict), "openlineage", "An OpenLineage event is a JSON object")
    run = event.get("run")
    at = _time(event.get("eventTime"))
    state = None
    if run is not None:
        require(isinstance(run, dict), "openlineage", "run is an object with a runId")
        run_id = _text(run.get("runId"), "run.runId", 200)
        state = event.get("eventType") or "OTHER"
        require(state in RUN_STATES, "openlineage", f"eventType is one of {', '.join(RUN_STATES)}, not {state!r}")
    if "dataset" in event and "job" not in event:  # a DatasetEvent: a dataset's own description, no job
        datasets, inputs, outputs, job = [_Dataset(event["dataset"], "dataset")], [_Dataset(event["dataset"], "dataset")], [], None
    else:
        job = event.get("job")
        require(isinstance(job, dict), "openlineage", "job is an object with a namespace and a name")
        job = f"{_text(job.get('namespace'), 'job.namespace')}/{_text(job.get('name'), 'job.name')}"
        raw_in, raw_out = event.get("inputs") or [], event.get("outputs") or []
        require(isinstance(raw_in, list) and isinstance(raw_out, list) and len(raw_in) + len(raw_out) <= LIMIT, "openlineage",
                f"inputs and outputs are lists of at most {LIMIT} datasets together")
        inputs, outputs = [_Dataset(d, "inputs") for d in raw_in], [_Dataset(d, "outputs") for d in raw_out]
        datasets = inputs + outputs
    kept, shape = _shape(event, datasets)
    entries = _entries(inputs, outputs) if run is None or state == "COMPLETE" else []
    digest = hashlib.sha256(json.dumps(kept, sort_keys=True).encode()).hexdigest()
    db = catalog.db
    with db.transaction():
        new = db.execute("""INSERT INTO lineage_events (digest, run_id, event_type, event_time, job, producer, body, received_by)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (digest) DO NOTHING RETURNING id""",
                         (digest, run_id if run is not None else None, state, at, job, str(event.get("producer") or "")[:500] or None,
                          Jsonb(kept), catalog.actor)).fetchone()
        if not new:
            stored = db.execute("SELECT state FROM executions WHERE system = 'openlineage' AND native_key = %s", (run_id,)).fetchone() if run is not None else None
            return {"run": run_id if run is not None else None, "state": stored["state"] if stored else state, "job": job, "change": "duplicate",
                    "entries": {"added": 0, "changed": 0, "unchanged": 0}}
        counts = catalog.apply(entries, merge=True)
        if run is not None:
            state = _record_run(db, run_id, run, state, at, job, inputs, outputs, shape)
    return {"run": run_id if run is not None else None, "state": state, "job": job, "change": "recorded", "entries": counts}


def _record_run(db, run_id: str, run: dict, state: str, at: datetime, job: str, inputs, outputs, shape) -> str:
    parent = ((run.get("facets") or {}).get("parent") or {}).get("run", {}).get("runId")
    db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"openlineage:{run_id}",))
    prior = db.execute("SELECT * FROM executions WHERE system = 'openlineage' AND native_key = %s", (run_id,)).fetchone()
    said = prior["evidence"] if prior else {}
    evidence = {"job": job,
                "inputs": sorted(set(said.get("inputs", [])) | {d.key for d in inputs}),
                "outputs": sorted(set(said.get("outputs", [])) | {d.key for d in outputs}),
                "parent": parent or said.get("parent"), "sql": shape or said.get("sql")}
    started = at if state == "START" or (prior is None and state not in FINISHED) else None
    ended = at if state in FINISHED else None
    if prior and prior["state"] in FINISHED and state not in FINISHED:
        state = prior["state"]  # a run that finished does not go back to running
    parent_id = db.execute("SELECT id FROM executions WHERE system = 'openlineage' AND native_key = %s", (parent,)).fetchone() if parent else None
    db.execute("""
        INSERT INTO executions (system, native_key, kind, parent_id, state, started_at, ended_at, evidence)
        VALUES ('openlineage', %(key)s, 'run', %(parent)s, %(state)s, %(started)s, %(ended)s, %(evidence)s)
        ON CONFLICT (system, native_key) DO UPDATE SET state = excluded.state,
          parent_id = COALESCE(executions.parent_id, excluded.parent_id),
          started_at = LEAST(executions.started_at, excluded.started_at), ended_at = GREATEST(executions.ended_at, excluded.ended_at),
          evidence = excluded.evidence, observed_at = now()""",
               {"key": run_id, "parent": parent_id["id"] if parent_id else None, "state": state, "started": started, "ended": ended,
                "evidence": Jsonb(evidence)})
    # children reported before their parent
    db.execute("""UPDATE executions c SET parent_id = p.id FROM executions p WHERE c.system = 'openlineage' AND c.parent_id IS NULL
                  AND c.evidence ->> 'parent' = %s AND p.system = 'openlineage' AND p.native_key = %s""", (run_id, run_id))
    return state


def read_events(path: str) -> list[dict]:
    """The events in a file: one JSON event, a JSON list of events, or one event per line (JSON Lines)."""
    with open(path, encoding="utf-8") as f:
        content = f.read()
    try:
        parsed = json.loads(content)
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        pass
    events = []
    for number, line in enumerate(content.splitlines(), 1):
        if line.strip():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise CatalogError("openlineage", f"{path} line {number} is not JSON: {e.msg}") from None
    return events
