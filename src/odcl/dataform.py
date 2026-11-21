"""Google Cloud Dataform into the catalog: what one workflow invocation compiled, and how each of its actions ran.

A Dataform action's definition and its execution are different claims. The definition comes from the invocation's
resolved compilation result: each table's file, the tables it depends on, and its compiled query, from which
`lineage` traces every column. The execution comes from the invocation's actions: state, times and the BigQuery job
ID. The job itself is joined from synced query history when the trace is read, so history may be synced before or
after the invocation is imported.

Every call is a free Dataform read: `workflowInvocations.get` and `.query`, `compilationResults.get` and `.query`.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime

from . import lineage
from .dbt import clean
from .gcp import refusal
from .store import Catalog, CatalogError, require

API = "https://dataform.googleapis.com/v1"
NAME = re.compile(r"^projects/([^/]+)/locations/([^/]+)/repositories/([^/]+)/workflowInvocations/([^/]+)$")
TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED", "SKIPPED", "DISABLED")
DIALECT = "bigquery"


class Client:
    """The four Dataform reads, over an authorized HTTP session."""

    def __init__(self, session):
        self.session = session

    def _get(self, url: str, params=None) -> dict:
        response = self.session.get(url, params=params, timeout=60)
        if not response.ok:
            raise CatalogError("dataform", f"Dataform refused the request for {url}: {refusal(response)}. Check the name and that you may read the repository")
        return response.json()

    def get(self, name: str) -> dict:
        return self._get(f"{API}/{name}")

    def query(self, name: str, field: str) -> list[dict]:
        """Every item of a `:query` listing, following page tokens."""
        out, token = [], None
        while True:
            page = self._get(f"{API}/{name}:query", {"pageSize": 1000, **({"pageToken": token} if token else {})})
            out += page.get(field, [])
            token = page.get("nextPageToken")
            if not token:
                return out


def client() -> Client:
    """A client using your application default credentials (`gcloud auth application-default login`)."""
    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
    except ImportError as e:  # pragma: no cover - depends on the installation
        raise CatalogError("dataform", "The Dataform importer needs google-auth: install open-data-context-layer[dataform]") from e
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return Client(AuthorizedSession(credentials))


def fqn(target: dict | None) -> str | None:
    """project.dataset.table for a Dataform target, lower-cased as the catalog keys tables."""
    return ".".join(target[k] for k in ("database", "schema", "name")).lower() if target else None


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


# ---- definitions ---------------------------------------------------------------------------------------------
class Definitions:
    """Builds the catalog entries for one compilation result, dependencies before what depends on them."""

    def __init__(self, catalog: Catalog, repository: str):
        self.catalog, self.repository = catalog, repository
        self.entries: list[tuple[str, dict]] = []
        self.tables: set[str] = set()
        self.columns: dict[str, set[str]] = {}  # table fqn -> column names registered in this batch
        self.traced: dict[str, dict] = {}       # target fqn -> {column name: "resolved" | "partial"}

    def known_columns(self, table: str) -> set[str]:
        prefix = f"column:{table}."
        known = {key[len(prefix):] for key in self.catalog._columns(f"table:{table}")} if self.catalog._head(f"table:{table}") else set()
        return {c for c in known | self.columns.get(table, set()) if "." not in c}

    def table(self, table: str, payload: dict | None = None) -> None:
        if payload is not None or (table not in self.tables and not self.catalog._head(f"table:{table}")):
            self.entries.append(("table", {"path": table, "system": "bigquery", **(payload or {})}))
        self.tables.add(table)

    def column(self, table: str, name: str, payload: dict | None = None) -> str:
        self.table(table)
        key = f"column:{table}.{name}"
        if payload is not None or (name not in self.columns.get(table, set()) and not self.catalog._head(key)):
            self.entries.append(("column", {"table": f"table:{table}", "name": name, **(payload or {})}))
        self.columns.setdefault(table, set()).add(name)
        return key

    def declaration(self, action: dict) -> None:
        described = (action["declaration"].get("relationDescriptor") or {})
        self.table(fqn(action["target"]), {"description": (described.get("description") or "")[:4000] or None,
                                           "source": {"dataform": {"repository": self.repository, "file": action.get("filePath"), "declared": True}}})

    def relation(self, action: dict) -> None:
        relation, target = action["relation"], fqn(action["target"])
        sql = relation.get("selectQuery") or ""
        described = relation.get("relationDescriptor") or {}
        notes = {".".join(c.get("path") or []).lower(): c.get("description") for c in described.get("columns") or []}
        reads = sorted({*(fqn(t) for t in relation.get("dependencyTargets") or []), *lineage.tables(sql, DIALECT)} - {target})
        for table in reads:
            self.table(table)
        self.table(target, {
            "description": (described.get("description") or "")[:4000] or None, "type": (relation.get("relationType") or "table").lower(),
            "derived_from": [f"table:{t}" for t in reads],
            "source": {"dataform": {"repository": self.repository, "file": action.get("filePath"), "canonical_target": fqn(action.get("canonicalTarget")),
                                    "query_sha256": hashlib.sha256(sql.encode()).hexdigest()}}})
        schema: dict = {}
        for table in reads:
            if not self.known_columns(table):  # a table whose columns are unknown is left for sqlglot to infer from the query
                continue
            level = schema
            *path, name = table.split(".")
            for part in path:
                level = level.setdefault(part, {})
            level[name] = {c: "unknown" for c in sorted(self.known_columns(table))}
        outputs = lineage.selected(sql, DIALECT, schema)
        traced: dict[str, str] = {}
        if not outputs:  # SQL nothing can read: whatever the catalog knew of these columns is no longer this definition
            for name in sorted(self.known_columns(target)):
                self.column(target, name, {"definition": "", "derived_from": [], "tags": ["lineage:partial"], "source": {"dataform": {"file": action.get("filePath")}}})
        for name in outputs:
            expression, parents, complete = None, [], False
            try:
                expression, parents, complete = lineage.column_parents(name, sql, DIALECT, schema)
            except Exception:  # noqa: BLE001 - a column sqlglot cannot trace is kept, marked as partial
                pass
            upstream = sorted({self.column(rel, clean(col)) for rel, col in parents if rel and rel != target})
            self.column(target, clean(name), {"definition": (expression or "")[:4000], "derived_from": upstream,
                                              "tags": [] if complete else ["lineage:partial"], "description": (notes.get(name) or "")[:4000] or None,
                                              "source": {"dataform": {"file": action.get("filePath")}}})
            traced[clean(name)] = "resolved" if complete else "partial"
        self.traced[target] = traced


def definition_entries(catalog: Catalog, repository: str, compiled: list[dict]) -> Definitions:
    """Entries for every declaration and table a compilation result defines, each after the tables it reads."""
    out = Definitions(catalog, repository)
    for action in compiled:
        if action.get("declaration") and action.get("target"):
            out.declaration(action)
    relations = {fqn(a["target"]): a for a in compiled if a.get("relation") and a.get("target")}
    done: set[str] = set()

    def visit(target: str, path: tuple = ()):
        if target in done or target in path:  # a cycle is Dataform's to report; register what can be
            return
        for dependency in relations[target]["relation"].get("dependencyTargets") or []:
            if fqn(dependency) in relations:
                visit(fqn(dependency), (*path, target))
        done.add(target)
        out.relation(relations[target])
    for target in sorted(relations):
        visit(target)
    return out


# ---- executions ----------------------------------------------------------------------------------------------
def _execution(db, row: dict) -> int:
    """Insert or refresh one run. A run that has finished keeps its outcome; a pinned definition is never replaced."""
    return db.execute(f"""
        INSERT INTO executions (system, native_key, kind, parent_id, definition_revision, state, started_at, ended_at,
                                job_project, job_location, job_id, evidence)
        VALUES ('dataform', %(native_key)s, %(kind)s, %(parent_id)s, %(definition_revision)s, %(state)s, %(started_at)s, %(ended_at)s,
                %(job_project)s, %(job_location)s, %(job_id)s, %(evidence)s)
        ON CONFLICT (system, native_key) DO UPDATE SET
          state = CASE WHEN executions.state IN {TERMINAL} THEN executions.state ELSE excluded.state END,
          started_at = COALESCE(executions.started_at, excluded.started_at),
          ended_at = COALESCE(executions.ended_at, excluded.ended_at),
          job_project = COALESCE(executions.job_project, excluded.job_project),
          job_location = COALESCE(executions.job_location, excluded.job_location),
          job_id = COALESCE(executions.job_id, excluded.job_id),
          definition_revision = COALESCE(executions.definition_revision, excluded.definition_revision),
          evidence = excluded.evidence || executions.evidence,
          observed_at = now()
        RETURNING id""", row).fetchone()["id"]


def import_invocation(catalog: Catalog, name: str, *, client) -> dict:
    """Register what an invocation's compilation result defines, and record the invocation and its actions as runs.

    Definitions are applied only when this compilation is at least as new as the newest one already imported for
    the repository, so importing an old invocation late never turns definitions back. Its runs are still
    recorded, without a pinned definition."""
    from psycopg.types.json import Jsonb
    found = NAME.match(name or "")
    require(found, "invocation", "Name an invocation as projects/P/locations/L/repositories/R/workflowInvocations/I")
    project, repository = found.group(1), name.split("/workflowInvocations/")[0]
    invocation = client.get(name)
    actions = client.query(name, "workflowInvocationActions")
    compilation_name = invocation.get("resolvedCompilationResult") or invocation.get("compilationResult")
    require(compilation_name, "invocation", f"{name} names no compilation result; it may have been started from a workflow config not yet resolved")
    compilation = client.get(compilation_name)
    compiled = client.query(compilation_name, "compilationResultActions")
    compiled_at = _time(compilation.get("createTime"))
    commit = compilation.get("resolvedGitCommitSha")  # a branch name in gitCommitish is not a commit, so it is not reported as one
    location = (compilation.get("codeCompilationConfig") or {}).get("defaultLocation")
    files = {fqn(a.get("target")): a.get("filePath") for a in compiled}
    db = catalog.db
    with db.transaction():
        catalog._lock(repository)
        newest = db.execute("""SELECT max((evidence ->> 'compiled_at')::timestamptz) AS at FROM executions
                               WHERE system = 'dataform' AND kind = 'invocation' AND evidence ->> 'repository' = %s AND native_key <> %s""",
                            (repository, name)).fetchone()["at"]
        current = newest is None or compiled_at is None or compiled_at >= newest
        definitions = definition_entries(catalog, repository, compiled) if current else None
        counts = catalog.apply(definitions.entries, merge=True) if current else {"added": 0, "changed": 0, "unchanged": 0}
        timing = invocation.get("invocationTiming") or {}
        parent = _execution(db, {
            "native_key": name, "kind": "invocation", "parent_id": None, "definition_revision": None, "state": invocation.get("state") or "UNKNOWN",
            "started_at": _time(timing.get("startTime")), "ended_at": _time(timing.get("endTime")), "job_project": None, "job_location": None, "job_id": None,
            "evidence": Jsonb({"repository": repository, "compilation_result": compilation_name, "commit": commit,
                               "compiled_at": compiled_at.isoformat() if compiled_at else None})})
        recorded = []
        for action in sorted(actions, key=lambda a: fqn(a.get("target")) or ""):
            target = fqn(action.get("target"))
            if not target:
                continue
            timing, job = action.get("invocationTiming") or {}, (action.get("bigqueryAction") or {}).get("jobId")
            pinned, head = {}, catalog._head(f"table:{target}") if current and target in definitions.traced else None
            if head:
                pinned = {"definition_revision": head["id"], "columns": {
                    column: {"key": f"column:{target}.{column}", "revision": catalog._head(f"column:{target}.{column}")["id"], "lineage": status}
                    for column, status in definitions.traced[target].items()}}
            _execution(db, {
                "native_key": f"{name}/actions/{target}", "kind": "action", "parent_id": parent, "definition_revision": pinned.get("definition_revision"),
                "state": action.get("state") or "UNKNOWN", "started_at": _time(timing.get("startTime")), "ended_at": _time(timing.get("endTime")),
                "job_project": project if job else None, "job_location": location if job else None, "job_id": job,
                "evidence": Jsonb({"target": target, "canonical_target": fqn(action.get("canonicalTarget")), "file": files.get(target),
                                   "commit": commit, "compilation_result": compilation_name, "repository": repository,
                                   **({"workspace": compilation["workspace"]} if compilation.get("workspace") else {}),
                                   **({"columns": pinned["columns"]} if pinned else {})})})
            recorded.append({"target": target, "state": action.get("state"), "job_id": job})
    return {"invocation": name, "compilation_result": compilation_name, "commit": commit, "entries": counts,
            "definitions": "applied" if current else "kept newer", "actions": recorded}
