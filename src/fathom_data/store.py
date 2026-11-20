"""Fathom: what data exists, how it connects, and which projects use it.

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

Fathom holds descriptions. It does not store or serve the data itself.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import sqlite3
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

KINDS = ("table", "column", "project")
PATH = re.compile(r"^[a-z0-9_][a-z0-9_.\-$]{0,299}$")
COLUMN = re.compile(r"^[a-z0-9_][a-z0-9_\-$]{0,149}$")
NAME = re.compile(r"^[a-z0-9_][a-z0-9_.\-]{0,99}$")
TEXT = {"table": {"system": 100, "layer": 50, "owner": 200, "type": 50},
        "column": {"dtype": 200, "definition": 4000, "owner": 200},
        "project": {"owner": 200, "repository": 500, "status": 50}}
HOW = ("direct", "column", "table", "downstream")  # how a project comes to depend on an entry, strongest first

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (key TEXT PRIMARY KEY, kind TEXT NOT NULL, head TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS revisions (id TEXT PRIMARY KEY, key TEXT NOT NULL, body TEXT NOT NULL, digest TEXT NOT NULL,
  actor TEXT NOT NULL, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS revisions_key ON revisions(key, at);
CREATE TABLE IF NOT EXISTS links (src TEXT NOT NULL, relation TEXT NOT NULL, dst TEXT NOT NULL, dst_revision TEXT NOT NULL,
  note TEXT, PRIMARY KEY (src, relation, dst));
CREATE INDEX IF NOT EXISTS links_dst ON links(dst, relation);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(key UNINDEXED, kind UNINDEXED, text);
"""


class FathomError(Exception):
    """A request Fathom refuses, with a short code a caller can branch on."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def require(ok, code: str, message: str) -> None:
    if not ok:
        raise FathomError(code, message)


def default_path() -> Path:
    return Path(os.environ.get("FATHOM_DB") or Path.home() / ".fathom" / "fathom.db")


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


class Fathom:
    """The whole interface: `put` and `apply` register entries, `use` records what a project used, and
    `find`, `get`, `lineage`, `used_by` and `uses` answer questions."""

    def __init__(self, path=None, actor: str | None = None):
        self.path = Path(path) if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.actor = (actor or os.environ.get("FATHOM_USER") or getpass.getuser())[:200]
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

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

    def _head(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT r.* FROM entries e JOIN revisions r ON r.id=e.head WHERE e.key=?", (key,)).fetchone()

    @staticmethod
    def _view(row) -> dict:
        return {**json.loads(row["body"]), "revision": row["id"], "registered_by": row["actor"], "at": row["at"]}

    # ---- writes ----------------------------------------------------------------------------------------------
    def put(self, kind: str, payload: dict) -> dict:
        """Register an entry, or a new revision of it. Saying exactly what is already registered changes nothing."""
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
            prior = self._head(key)
            links = [link for link in json.loads(prior["body"])["links"] if link["relation"] == "uses"] if prior else []
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
        """Register many (kind, payload) entries in order, as an importer produces them."""
        counts = {"added": 0, "changed": 0, "unchanged": 0}
        for kind, payload in entries:
            counts[self.put(kind, payload)["change"]] += 1
        return counts

    def use(self, project: str, keys: list[str], purpose: str | None = None) -> dict:
        """Record that a project used these tables or columns, at the revision each has now."""
        project = self.resolve(project, ("project",))
        purpose = text(purpose, "purpose", 500, optional=True)
        body = json.loads(self._head(project)["body"])
        require(keys, "keys", "Name at least one table or column")
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
        because recording use again means the project has seen the current definition."""
        said = {**body, "links": [{k: v for k, v in link.items() if k != "revision" or link["relation"] == "uses"} for link in body["links"]]}
        digest = hashlib.sha256(json.dumps(said, sort_keys=True).encode()).hexdigest()
        head = self._head(body["key"])
        if head and head["digest"] == digest:
            return {**self._view(head), "change": "unchanged"}
        revision, at = "rev_" + uuid.uuid4().hex[:16], datetime.now(timezone.utc).isoformat(timespec="seconds")
        words = " ".join(str(body.get(f) or "") for f in ("title", "description", "definition", "owner", "layer", "system", "dtype"))
        with self.db:
            self.db.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?)", (revision, body["key"], json.dumps(body), digest, self.actor, at))
            self.db.execute("INSERT INTO entries VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET head=excluded.head", (body["key"], body["kind"], revision))
            self.db.execute("DELETE FROM links WHERE src=?", (body["key"],))
            self.db.executemany("INSERT INTO links VALUES (?,?,?,?,?)",
                                [(body["key"], link["relation"], link["to"], link["revision"], link.get("note")) for link in body["links"]])
            self.db.execute("DELETE FROM search WHERE key=?", (body["key"],))
            self.db.execute("INSERT INTO search VALUES (?,?,?)", (body["key"], body["kind"], words + " " + " ".join(body["tags"])))
        return {**self._view(self._head(body["key"])), "change": "changed" if head else "added"}

    # ---- reads -----------------------------------------------------------------------------------------------
    def _columns(self, table: str) -> list[str]:
        rows = self.db.execute("SELECT e.key, r.body FROM entries e JOIN revisions r ON r.id=e.head WHERE e.kind='column' AND e.key LIKE ? ESCAPE '\\' ORDER BY e.key",
                               ("column:" + table[6:].replace("_", "\\_") + ".%",))
        return [r["key"] for r in rows if json.loads(r["body"])["table"] == table]  # a.b's columns, not those of a.b.c

    def _summary(self, key: str, users=False) -> dict:
        row = self._head(key)
        body = json.loads(row["body"])
        table = json.loads(self._head(body["table"])["body"]) if body["kind"] == "column" else body
        out = {"key": key, "kind": body["kind"], "title": body["title"], "description": body["description"], "owner": body.get("owner") or table.get("owner"),
               "revision": row["id"]}
        if body["kind"] != "project":
            out.update(layer=table.get("layer"), system=table.get("system"), dtype=body.get("dtype"))
            if users:
                out["used_by"] = len(self._users(key))
        return out

    def _linked(self, key: str, relation: str, direction: str) -> list[dict]:
        """Entries one link away: what `key` points at (up), or what points at it (down)."""
        mine, other = ("src", "dst") if direction == "up" else ("dst", "src")
        rows = self.db.execute(f"SELECT l.*, e.head FROM links l JOIN entries e ON e.key=l.dst WHERE l.{mine}=? AND l.relation=? ORDER BY l.{other}",
                               (key, relation)).fetchall()
        return [{**self._summary(r[other]), "linked_revision": r["dst_revision"], "changed_since": r["dst_revision"] != r["head"],
                 **({"purpose": r["note"]} if relation == "uses" else {})} for r in rows]

    def _walk(self, key: str, direction: str) -> list[dict]:
        """Every entry reachable along lineage from `key`, nearest first."""
        mine, other = ("src", "dst") if direction == "up" else ("dst", "src")
        seen, out, queue = {key}, [], deque([(key, 0)])
        while queue:
            current, depth = queue.popleft()
            for r in self.db.execute(f"SELECT l.*, e.head FROM links l JOIN entries e ON e.key=l.dst WHERE l.{mine}=? AND l.relation='derived_from' ORDER BY l.{other}",
                                     (current,)):
                if r[other] not in seen:
                    seen.add(r[other])
                    out.append({**self._summary(r[other]), "depth": depth + 1, "via": current, "changed_since": r["dst_revision"] != r["head"]})
                    queue.append((r[other], depth + 1))
        return out

    def _users(self, key: str) -> list[dict]:
        """Projects that depend on a table or column, each once, by the strongest way it does."""
        kind = key.split(":", 1)[0]
        table = json.loads(self._head(key)["body"])["table"] if kind == "column" else key
        reach = [(key, "direct")]
        if kind == "column":
            reach.append((table, "table"))
        else:
            reach += [(column, "column") for column in self._columns(key)]
        for node in self._walk(key, "down"):
            reach.append((node["key"], "downstream"))
            if node["kind"] == "column":
                reach.append((json.loads(self._head(node["key"])["body"])["table"], "downstream"))
        found: dict[str, dict] = {}
        for through, how in reach:
            for r in self.db.execute("SELECT l.*, e.head FROM links l JOIN entries e ON e.key=l.dst WHERE l.dst=? AND l.relation='uses'", (through,)):
                if r["src"] not in found or HOW.index(how) < HOW.index(found[r["src"]]["how"]):
                    found[r["src"]] = {"project": r["src"], "how": how, "through": through, "purpose": r["note"],
                                       "changed_since": r["dst_revision"] != r["head"]}
        return [found[k] for k in sorted(found, key=lambda k: (HOW.index(found[k]["how"]), k))]

    def find(self, query: str = "", kind: str | None = None, layer: str | None = None, limit: int = 50) -> list[dict]:
        """Entries whose name, description, definition, owner or tags hold every word, best match first."""
        require(kind is None or kind in KINDS, "kind", f"kind is one of: {', '.join(KINDS)}")
        require(isinstance(query, str) and len(query) <= 300, "query", "Search with at most 300 characters")
        words = re.findall(r"[A-Za-z0-9]+", query)
        if words:
            rows = self.db.execute("SELECT key FROM search WHERE search MATCH ? ORDER BY bm25(search), key LIMIT 2000",
                                   (" ".join(f'"{w}"*' for w in words),)).fetchall()
        else:
            rows = self.db.execute("SELECT key FROM entries ORDER BY key LIMIT 2000").fetchall()
        out = []
        for r in rows:
            if kind and not r["key"].startswith(kind + ":"):
                continue
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
        out["history"] = [dict(r) for r in self.db.execute("SELECT id AS revision, actor AS registered_by, at FROM revisions WHERE key=? ORDER BY at DESC, rowid DESC", (key,))]
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

    def uses(self, name: str, upstream: bool = False) -> dict:
        """What a project used. With `upstream`, also everything that data is built from."""
        key = self.resolve(name, ("project",))
        used = self._linked(key, "uses", "up")
        out = {**self._summary(key), "uses": used}
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
        stale = [{"project": r["src"], "key": r["dst"]} for r in self.db.execute(
            "SELECT l.src, l.dst FROM links l JOIN entries e ON e.key=l.dst WHERE l.relation='uses' AND l.dst_revision<>e.head ORDER BY l.src, l.dst")]
        return {"counts": counts, "changed_since_used": stale, "database": str(self.path)}
