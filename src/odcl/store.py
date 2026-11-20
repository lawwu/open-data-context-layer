"""Open Data Context Layer: what data exists, how it connects, and which projects use it.

Three kinds of entry, each versioned and attributed:

  table     a table, view or file in some system              table:<database>.<schema>.<name>
  column    one column of a registered table                  column:<table path>.<name>
  project   anything that uses data: an analysis, a model     project:<name>

Data is registered once and shared; it does not belong to a project. Two links, both pointing at the thing
relied on:

  derived_from   table -> tables, column -> columns it is computed from (lineage)
  uses           project -> the tables and columns it used, with what for

A `uses` link remembers the revision of the data the project saw, so a project built on a definition that has
since changed is reported, not assumed current.

The catalog holds descriptions. It does not store or serve the data itself. Everything it keeps lives in one
PostgreSQL database, named by `ODCL_DATABASE_URL`.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import uuid
from collections import deque
from datetime import datetime, timezone
from importlib import resources

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

KINDS = ("table", "column", "project")
PATH = re.compile(r"^[a-z0-9_][a-z0-9_.\-$]{0,299}$")
COLUMN = re.compile(r"^[a-z0-9_][a-z0-9_\-$]{0,149}$")
NAME = re.compile(r"^[a-z0-9_][a-z0-9_.\-]{0,99}$")
TEXT = {"table": {"system": 100, "layer": 50, "owner": 200, "type": 50},
        "column": {"dtype": 200, "definition": 4000, "owner": 200},
        "project": {"owner": 200, "repository": 500, "status": 50}}
HOW = ("direct", "column", "table", "downstream")  # how a project comes to depend on an entry, strongest first

MIGRATIONS_LOCK = 0x6F64636C  # "odcl": one migration run at a time per database
TOKEN = re.compile(r"[a-z0-9]+")


def migrations() -> list[tuple[int, str, str]]:
    """(version, name, sql) for every migration shipped with the package, in order."""
    files = sorted(f for f in resources.files("odcl").joinpath("migrations").iterdir() if f.name.endswith(".sql"))
    return [(int(f.name.split("_", 1)[0]), f.name[:-4], f.read_text(encoding="utf-8")) for f in files]


def database_url(dsn: str | None = None) -> str:
    dsn = dsn or os.environ.get("ODCL_DATABASE_URL")
    require(dsn, "database", "Name the PostgreSQL database: set ODCL_DATABASE_URL or pass --database-url")
    return dsn


def connect(dsn: str | None, **options) -> psycopg.Connection:
    try:
        return psycopg.connect(database_url(dsn), row_factory=dict_row, **options)
    except psycopg.OperationalError as e:
        raise CatalogError("database", f"Cannot reach the database: {str(e).strip().splitlines()[-1]}") from None


def migrate(dsn: str | None = None) -> list[str]:
    """Bring the database up to date. Every pending migration applies in one transaction, or none does."""
    with connect(dsn) as db:
        db.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATIONS_LOCK,))
        db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version integer PRIMARY KEY, name text NOT NULL, "
                   "applied_at timestamptz NOT NULL DEFAULT now())")
        done = {r["version"] for r in db.execute("SELECT version FROM schema_migrations")}
        applied = []
        for version, name, sql in migrations():
            if version not in done:
                db.execute(sql)
                db.execute("INSERT INTO schema_migrations (version, name) VALUES (%s, %s)", (version, name))
                applied.append(name)
    return applied


def stamp(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


def search_text(body: dict) -> str:
    """The words an entry is found by, split the same way a query is."""
    words = " ".join(str(body.get(f) or "") for f in ("title", "description", "definition", "owner", "layer", "system", "dtype"))
    return " ".join(TOKEN.findall((words + " " + " ".join(body["tags"])).lower()))


class CatalogError(Exception):
    """A request the catalog refuses, with a short code a caller can branch on."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def require(ok, code: str, message: str) -> None:
    if not ok:
        raise CatalogError(code, message)


def text(value, field: str, limit: int, optional=False) -> str | None:
    if optional and value in (None, ""):
        return None
    require(isinstance(value, str) and value.strip() and len(value) <= limit, field, f"{field} is text of at most {limit} characters")
    return value.strip()


def names(value, field: str, limit=200) -> list[str]:
    value = [] if value is None else value
    require(isinstance(value, list) and len(value) <= limit and all(isinstance(v, str) and v.strip() for v in value), field,
            f"{field} is a list of at most {limit} keys")
    return sorted({v.strip() for v in value})


class Catalog:
    """The whole interface: `put` and `apply` register entries, `use` records what a project used, and
    `find`, `get`, `lineage`, `used_by` and `uses` answer questions. `migrate` prepares the database first."""

    def __init__(self, dsn: str | None = None, actor: str | None = None):
        self.actor = (actor or os.environ.get("ODCL_USER") or os.environ.get("FATHOM_USER") or getpass.getuser())[:200]
        self.db = connect(dsn, autocommit=True)
        try:
            version = self.db.execute("SELECT max(version) AS version FROM schema_migrations").fetchone()["version"] or 0
        except psycopg.errors.UndefinedTable:
            version = 0
        latest = migrations()[-1][0]
        if version != latest:
            self.db.close()
            raise CatalogError("schema", f"The database is at schema version {version} and this release needs {latest}. "
                               + ("Run `odcl migrate`" if version < latest else "Upgrade odcl"))

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- keys ------------------------------------------------------------------------------------------------
    def resolve(self, name: str, kinds=KINDS) -> str:
        """The key for what a person typed: a full key, or a path or name that matches exactly one entry."""
        require(isinstance(name, str) and name.strip(), "key", "Name an entry")
        name = name.strip().lower()
        if name.split(":", 1)[0] in KINDS and ":" in name:
            found = [name] if self._head(name) and name.split(":", 1)[0] in kinds else []
        else:
            found = [f"{k}:{name}" for k in kinds if self._head(f"{k}:{name}")]
        require(found, "not_found", f"Nothing registered as {name}")
        require(len(found) == 1, "ambiguous", f"{name} could be {' or '.join(found)}; give the full key")
        return found[0]

    def _head(self, key: str) -> dict | None:
        return self.db.execute("SELECT r.* FROM entries e JOIN revisions r ON r.id=e.head_revision WHERE e.key=%s", (key,)).fetchone()

    def _lock(self, key: str) -> None:
        """Hold an entry, registered yet or not, until the transaction ends, so its head is compared and moved by one writer at a time."""
        self.db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))

    @staticmethod
    def _view(row) -> dict:
        return {**row["body"], "revision": row["id"], "registered_by": row["actor"], "at": stamp(row["recorded_at"])}

    # ---- writes ----------------------------------------------------------------------------------------------
    def put(self, kind: str, payload: dict) -> dict:
        """Register an entry, or a new revision of it. Saying exactly what is already registered changes nothing."""
        with self.db.transaction():
            require(kind in KINDS, "kind", f"An entry is one of: {', '.join(KINDS)}")
            require(isinstance(payload, dict), "payload", "An entry is described by an object of fields")
            p, links = payload, []
            if kind == "table":
                path = str(p.get("path") or "").lower().removeprefix("table:")
                require(PATH.match(path), "path", "A table path looks like database.schema.name (letters, digits, _ - $ and dots)")
                key, title = f"table:{path}", path
                extra = {"path": path}
            elif kind == "column":
                table = self.resolve(str(p.get("table") or ""), ("table",))
                name = str(p.get("name") or "").lower()
                require(COLUMN.match(name), "name", "A column name is letters, digits, _ - or $")
                key, title = f"column:{table[6:]}.{name}", f"{table[6:]}.{name}"
                extra = {"table": table, "name": name}
            else:
                name = str(p.get("name") or "").lower()
                require(NAME.match(name), "name", "A project name is letters, digits, _ . or -")
                key, title = f"project:{name}", name
                extra = {"name": name}
                self._lock(key)
                prior = self._head(key)
                links = [link for link in prior["body"]["links"] if link["relation"] == "uses"] if prior else []
            if kind != "project":
                for parent in names(p.get("derived_from"), "derived_from"):
                    parent = self.resolve(parent, (kind,))
                    require(parent != key, "derived_from", f"{key} cannot be derived from itself")
                    links.append({"relation": "derived_from", "to": parent, "revision": self._head(parent)["id"]})
            tags = p.get("tags") or []
            require(isinstance(tags, list) and len(tags) <= 30 and all(isinstance(t, str) and 0 < len(t) <= 60 for t in tags), "tags", "Use at most 30 short tags")
            source = p.get("source") or {}
            require(isinstance(source, dict) and len(json.dumps(source)) <= 5000, "source", "source is a small object saying where the entry came from")
            body = {"key": key, "kind": kind, "title": title, **extra,
                    "description": text(p.get("description"), "description", 4000, optional=kind != "project") or "",
                    **{field: text(p.get(field), field, limit, optional=True) for field, limit in TEXT[kind].items()},
                    "tags": sorted(set(tags)), "source": source, "links": links}
            return self._write(body)

    def apply(self, entries) -> dict:
        """Register many (kind, payload) entries in order, as an importer produces them. All of them, or none."""
        counts = {"added": 0, "changed": 0, "unchanged": 0}
        with self.db.transaction():
            for kind, payload in entries:
                counts[self.put(kind, payload)["change"]] += 1
        return counts

    def use(self, project: str, keys: list[str], purpose: str | None = None) -> dict:
        """Record that a project used these tables or columns, at the revision each has now."""
        purpose = text(purpose, "purpose", 500, optional=True)
        require(keys, "keys", "Name at least one table or column")
        with self.db.transaction():
            project = self.resolve(project, ("project",))
            self._lock(project)
            body = self._head(project)["body"]
            kept = {link["to"]: link for link in body["links"]}
            for name in keys:
                key = self.resolve(name, ("table", "column"))
                kept[key] = {"relation": "uses", "to": key, "revision": self._head(key)["id"],
                             "note": purpose if purpose is not None else kept.get(key, {}).get("note")}
            body["links"] = [kept[k] for k in sorted(kept)]
            return self._write(body)

    def _write(self, body: dict) -> dict:
        """Store a revision unless it says what the head says. A lineage link's pinned revision is not part of what
        an entry says, so re-registering a table does not ripple down everything built from it; a `uses` link's is,
        because recording use again means the project has seen the current definition. Returning to an older
        wording is a new revision: only the head is compared."""
        said = {**body, "links": [{k: v for k, v in link.items() if k != "revision" or link["relation"] == "uses"} for link in body["links"]]}
        digest = hashlib.sha256(json.dumps(said, sort_keys=True).encode()).hexdigest()
        with self.db.transaction():
            self._lock(body["key"])
            head = self._head(body["key"])
            if head and head["digest"] == digest:
                return {**self._view(head), "change": "unchanged"}
            revision, at = "rev_" + uuid.uuid4().hex[:16], datetime.now(timezone.utc).replace(microsecond=0)
            self._store(revision, body, digest, self.actor, at)
            self.db.execute("INSERT INTO entries (key, kind, head_revision, search) VALUES (%s, %s, %s, to_tsvector('simple', %s)) "
                            "ON CONFLICT (key) DO UPDATE SET head_revision = excluded.head_revision, search = excluded.search",
                            (body["key"], body["kind"], revision, search_text(body)))
        return {**self._view(self._head(body["key"])), "change": "changed" if head else "added"}

    def _store(self, revision: str, body: dict, digest: str, actor: str, at: datetime) -> None:
        """One immutable revision and the links it pins. The caller moves the head in the same transaction."""
        self.db.execute("INSERT INTO revisions (id, entry_key, body, digest, actor, recorded_at) VALUES (%s, %s, %s, %s, %s, %s)",
                        (revision, body["key"], Jsonb(body), digest, actor, at))
        with self.db.cursor() as cursor:
            cursor.executemany("INSERT INTO revision_links (source_revision, relation, target_key, target_revision, purpose) VALUES (%s, %s, %s, %s, %s)",
                               [(revision, link["relation"], link["to"], link["revision"], link.get("note")) for link in body["links"]])

    # ---- reads -----------------------------------------------------------------------------------------------
    def _columns(self, table: str) -> list[str]:
        rows = self.db.execute("SELECT e.key FROM entries e JOIN revisions r ON r.id = e.head_revision "
                               "WHERE e.kind = 'column' AND r.body ->> 'kind' = 'column' AND r.body ->> 'table' = %s ORDER BY e.key", (table,))
        return [r["key"] for r in rows]

    def _summary(self, key: str, users=False) -> dict:
        row = self._head(key)
        body = row["body"]
        table = self._head(body["table"])["body"] if body["kind"] == "column" else body
        out = {"key": key, "kind": body["kind"], "title": body["title"], "description": body["description"], "owner": body.get("owner") or table.get("owner"),
               "revision": row["id"]}
        if body["kind"] != "project":
            out.update(layer=table.get("layer"), system=table.get("system"), dtype=body.get("dtype"))
            if users:
                out["used_by"] = len(self._users(key))
        return out

    def _linked(self, key: str, relation: str, direction: str, revision: str | None = None) -> list[dict]:
        """Entries one link away: what `key` points at (up), or what points at it (down). Up can be asked of an
        older revision of `key`; it answers with what that revision pinned."""
        if direction == "up":
            rows = self.db.execute("SELECT l.target_key AS other, l.target_revision, l.purpose, t.head_revision AS target_head FROM revision_links l "
                                   "JOIN entries t ON t.key = l.target_key WHERE l.source_revision = %s AND l.relation = %s ORDER BY l.target_key",
                                   (revision or self._head(key)["id"], relation)).fetchall()
        else:
            rows = self.db.execute("SELECT source_key AS other, target_revision, purpose, target_head FROM current_links "
                                   "WHERE target_key = %s AND relation = %s ORDER BY source_key", (key, relation)).fetchall()
        return [{**self._summary(r["other"]), "linked_revision": r["target_revision"], "changed_since": r["target_revision"] != r["target_head"],
                 **({"purpose": r["purpose"]} if relation == "uses" else {})} for r in rows]

    def _walk(self, key: str, direction: str) -> list[dict]:
        """Every entry reachable along lineage from `key`, nearest first. One recursive query reads the edges."""
        mine, other = ("source_key", "target_key") if direction == "up" else ("target_key", "source_key")
        edges: dict[str, list[dict]] = {}
        for r in self.db.execute(f"""
                WITH RECURSIVE reach(key) AS (
                  SELECT %s::text COLLATE "C"
                  UNION
                  SELECT l.{other} FROM reach JOIN current_links l ON l.{mine} = reach.key AND l.relation = 'derived_from')
                SELECT l.{mine} AS near, l.{other} AS far, l.target_revision, l.target_head
                FROM reach JOIN current_links l ON l.{mine} = reach.key AND l.relation = 'derived_from'
                ORDER BY l.{other}""", (key,)):
            edges.setdefault(r["near"], []).append(r)
        seen, out, queue = {key}, [], deque([(key, 0)])
        while queue:
            current, depth = queue.popleft()
            for r in edges.get(current, []):
                if r["far"] not in seen:
                    seen.add(r["far"])
                    out.append({**self._summary(r["far"]), "depth": depth + 1, "via": current, "changed_since": r["target_revision"] != r["target_head"]})
                    queue.append((r["far"], depth + 1))
        return out

    def _users(self, key: str) -> list[dict]:
        """Projects that depend on a table or column, each once, by the strongest way it does."""
        kind = key.split(":", 1)[0]
        table = self._head(key)["body"]["table"] if kind == "column" else key
        reach = [(key, "direct")]
        if kind == "column":
            reach.append((table, "table"))
        else:
            reach += [(column, "column") for column in self._columns(key)]
        for node in self._walk(key, "down"):
            reach.append((node["key"], "downstream"))
            if node["kind"] == "column":
                reach.append((self._head(node["key"])["body"]["table"], "downstream"))
        using: dict[str, list[dict]] = {}
        for r in self.db.execute("SELECT source_key, target_key, target_revision, purpose, target_head FROM current_links "
                                 "WHERE relation = 'uses' AND target_key = ANY(%s) ORDER BY source_key", (sorted({through for through, _ in reach}),)):
            using.setdefault(r["target_key"], []).append(r)
        found: dict[str, dict] = {}
        for through, how in reach:
            for r in using.get(through, []):
                if r["source_key"] not in found or HOW.index(how) < HOW.index(found[r["source_key"]]["how"]):
                    found[r["source_key"]] = {"project": r["source_key"], "how": how, "through": through, "purpose": r["purpose"],
                                              "changed_since": r["target_revision"] != r["target_head"]}
        return [found[k] for k in sorted(found, key=lambda k: (HOW.index(found[k]["how"]), k))]

    def find(self, query: str = "", kind: str | None = None, layer: str | None = None, limit: int = 50) -> list[dict]:
        """Entries whose name, description, definition, owner or tags hold every word, best match first."""
        require(kind is None or kind in KINDS, "kind", f"kind is one of: {', '.join(KINDS)}")
        require(isinstance(query, str) and len(query) <= 300, "query", "Search with at most 300 characters")
        words = TOKEN.findall(query.lower())
        if words:
            rows = self.db.execute("SELECT e.key FROM entries e, to_tsquery('simple', %s) q WHERE e.search @@ q AND (%s::text IS NULL OR e.kind = %s) "
                                   "ORDER BY ts_rank(e.search, q) DESC, e.key LIMIT 2000", (" & ".join(w + ":*" for w in words), kind, kind)).fetchall()
        else:
            rows = self.db.execute("SELECT key FROM entries WHERE (%s::text IS NULL OR kind = %s) ORDER BY key LIMIT 2000", (kind, kind)).fetchall()
        out = []
        for r in rows:
            summary = self._summary(r["key"], users=True)
            if layer and summary.get("layer") != layer:
                continue
            out.append(summary)
            if len(out) >= limit:
                break
        return out

    def get(self, name: str) -> dict:
        """One entry in full: what it says, what it is built from, what is built from it, who uses it, and its history."""
        key = self.resolve(name)
        out = self._view(self._head(key))
        del out["links"]
        out["history"] = [{"revision": r["id"], "registered_by": r["actor"], "at": stamp(r["recorded_at"])} for r in self.db.execute(
            "SELECT id, actor, recorded_at FROM revisions WHERE entry_key = %s ORDER BY recorded_at DESC, seq DESC", (key,))]
        if out["kind"] == "project":
            out["uses"] = self._linked(key, "uses", "up")
            return out
        out.update(derived_from=self._linked(key, "derived_from", "up"), feeds=self._linked(key, "derived_from", "down"), used_by=self._users(key))
        if out["kind"] == "table":
            out["columns"] = [self._summary(column) for column in self._columns(key)]
        else:
            out["layer"] = self._summary(key)["layer"]
        return out

    def lineage(self, name: str, direction: str = "both") -> dict:
        """Upstream: what a table or column is built from. Downstream: what a change to it reaches."""
        require(direction in ("up", "down", "both"), "direction", "direction is up, down or both")
        key = self.resolve(name, ("table", "column"))
        out = self._summary(key)
        if direction in ("up", "both"):
            out["upstream"] = self._walk(key, "up")
        if direction in ("down", "both"):
            out["downstream"] = self._walk(key, "down")
        return out

    def used_by(self, name: str) -> dict:
        """Which projects depend on a table or column: directly, through its table or columns, or through data built from it."""
        key = self.resolve(name, ("table", "column"))
        projects = [{**u, "title": self._summary(u["project"])["title"], "owner": self._summary(u["project"])["owner"]} for u in self._users(key)]
        return {**self._summary(key), "projects": projects}

    def uses(self, name: str, upstream: bool = False, revision: str | None = None) -> dict:
        """What a project used. With `upstream`, also everything that data is built from. With `revision`, what an
        older revision of the project had pinned; `changed_since` still compares with the data as it is now."""
        key = self.resolve(name, ("project",))
        if revision is not None:
            known = self.db.execute("SELECT 1 FROM revisions WHERE id = %s AND entry_key = %s", (revision, key)).fetchone()
            require(known, "not_found", f"{key} has no revision {revision}")
        used = self._linked(key, "uses", "up", revision)
        out = {**self._summary(key), **({"at_revision": revision} if revision else {}), "uses": used}
        if upstream:
            seen, reached = {u["key"] for u in used}, []
            for u in used:
                for node in self._walk(u["key"], "up"):
                    if node["key"] not in seen:
                        seen.add(node["key"])
                        reached.append({k: v for k, v in node.items() if k not in ("depth", "via", "changed_since")})
            out["built_from"] = sorted(reached, key=lambda n: n["key"])
        return out

    def overview(self) -> dict:
        counts = {kind: 0 for kind in KINDS}
        counts.update({r["kind"]: r["n"] for r in self.db.execute("SELECT kind, COUNT(*) AS n FROM entries GROUP BY kind")})
        stale = [{"project": r["source_key"], "key": r["target_key"]} for r in self.db.execute(
            "SELECT source_key, target_key FROM current_links WHERE relation = 'uses' AND target_revision <> target_head ORDER BY source_key, target_key")]
        info = self.db.info
        return {"counts": counts, "changed_since_used": stale, "database": f"{info.host}:{info.port}/{info.dbname}"}
