"""Bring a Fathom SQLite catalog into the Postgres catalog, once.

The SQLite file is opened read-only. Every revision keeps its ID, author and time. The old `links` table described
only each entry's head, so the links of every revision, old ones included, are rebuilt from the body that revision
stored. The whole import is one transaction: a file that does not hold together imports nothing.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import psycopg

from .store import Catalog, require, search_text


def import_sqlite(catalog: Catalog, path) -> dict:
    """Copy every entry, revision and pinned link from the SQLite file at `path` into an empty catalog.

    Answers with what was written and what the file held, having checked that the two agree."""
    path = Path(path)
    require(path.is_file(), "sqlite", f"No SQLite catalog at {path}")
    old = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    old.row_factory = sqlite3.Row
    try:
        try:
            entries = old.execute("SELECT key, kind, head FROM entries ORDER BY key").fetchall()
            revisions = old.execute("SELECT id, key, body, digest, actor, at FROM revisions ORDER BY rowid").fetchall()
            head_links = {tuple(r) for r in old.execute("SELECT src, relation, dst, dst_revision FROM links")}
        except sqlite3.DatabaseError as e:
            require(False, "sqlite", f"{path} is not a Fathom catalog: {e}")
    finally:
        old.close()
    bodies = {r["id"]: json.loads(r["body"]) for r in revisions}
    db = catalog.db
    try:
        with db.transaction():
            db.execute("LOCK TABLE entries IN EXCLUSIVE MODE")
            require(db.execute("SELECT count(*) AS n FROM entries").fetchone()["n"] == 0, "not_empty",
                    "The catalog already holds entries. A SQLite catalog is imported once, into an empty database")
            for r in entries:
                require(r["head"] in bodies and bodies[r["head"]]["key"] == r["key"], "sqlite", f"{r['key']} has no head revision in {path}")
                db.execute("INSERT INTO entries (key, kind, head_revision, search) VALUES (%s, %s, %s, to_tsvector('simple', %s))",
                           (r["key"], r["kind"], r["head"], search_text(bodies[r["head"]])))
            for r in revisions:  # a revision's own row before any link to it, so every pin can be checked as it lands
                db.execute("INSERT INTO revisions (id, entry_key, body, digest, actor, recorded_at) VALUES (%s, %s, %s, %s, %s, %s)",
                           (r["id"], r["key"], psycopg.types.json.Jsonb(bodies[r["id"]]), r["digest"], r["actor"], datetime.fromisoformat(r["at"])))
            links = 0
            with db.cursor() as cursor:
                for r in revisions:
                    for link in bodies[r["id"]]["links"]:
                        cursor.execute("INSERT INTO revision_links (source_revision, relation, target_key, target_revision, purpose) VALUES (%s, %s, %s, %s, %s)",
                                       (r["id"], link["relation"], link["to"], link["revision"], link.get("note")))
                        links += 1
            current = {(r["source_key"], r["relation"], r["target_key"], r["target_revision"]) for r in db.execute("SELECT * FROM current_links")}
            require(current == head_links, "sqlite", f"The links rebuilt from revision bodies differ from the links table of {path} "
                    f"({len(current ^ head_links)} differ); nothing was imported")
    except psycopg.errors.IntegrityError as e:
        require(False, "sqlite", f"{path} does not hold together, nothing was imported: {e.diag.message_detail or e.diag.message_primary}")
    return {"entries": len(entries), "revisions": len(revisions), "links": links, "current_links": len(head_links), "source": str(path)}
