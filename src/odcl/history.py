"""Reading the catalog's history: entries, their revisions, and what each revision pinned.

Every entry has revisions, kept forever, in the order they were written (`seq`). An entry's head is its newest.
A revision's links pin the revision of what they point at. This module is the one place that knows how those are
stored (the `entries`, `revisions` and `revision_links` tables), for the modules that ask about the past: what an
entry said at a time, what changed between two points, when a project began using something.

`catalog.history` is a `History`. A *point* is a place in the order of all revisions (a `seq`): `point(revision)`,
`point_at(time)` or `now()`. A *revision row* has `id`, `entry_key`, `seq`, `recorded_at`, `actor` and `body`.
"""
from __future__ import annotations

from datetime import datetime


class History:
    def __init__(self, db):
        self.db = db

    # ---- entries as they are now ----------------------------------------------------------------------------
    def entry(self, key: str) -> dict | None:
        """An entry's head revision row, or None when nothing is registered under the key."""
        return self.db.execute("SELECT r.* FROM entries e JOIN revisions r ON r.id = e.head_revision WHERE e.key = %s", (key,)).fetchone()

    def entries(self, keys) -> dict[str, dict]:
        """The head revision rows of many entries, by key, in one query. Keys not registered are left out."""
        return {r["entry_key"]: r for r in self.db.execute(
            "SELECT r.* FROM entries e JOIN revisions r ON r.id = e.head_revision WHERE e.key = ANY(%s)", (list(set(keys)),))}

    def head(self, key: str) -> str | None:
        """The id of an entry's head revision, or None."""
        row = self.db.execute("SELECT head_revision FROM entries WHERE key = %s", (key,)).fetchone()
        return row["head_revision"] if row else None

    def known(self, keys) -> set[str]:
        """Which of these keys are registered."""
        return {r["key"] for r in self.db.execute("SELECT key FROM entries WHERE key = ANY(%s)", (list(set(keys)),))}

    def keys(self, kinds) -> list[str]:
        """Every key of these kinds of entry, in order."""
        return [r["key"] for r in self.db.execute("SELECT key FROM entries WHERE kind = ANY(%s) ORDER BY key", (list(kinds),))]

    def columns(self, table: str, retired: bool = True) -> list[dict]:
        """The head revision rows of a table's columns, by key. Retired columns only with `retired`."""
        return self.db.execute("""SELECT r.* FROM entries e JOIN revisions r ON r.id = e.head_revision
                                  WHERE e.kind = 'column' AND r.body ->> 'kind' = 'column' AND r.body ->> 'table' = %s
                                    AND (%s OR NOT (r.body ? 'retired')) ORDER BY e.key""", (table, retired)).fetchall()

    # ---- revisions --------------------------------------------------------------------------------------------
    def revision(self, revision: str, key: str | None = None) -> dict | None:
        """One revision row by id; with `key`, only if it is a revision of that entry."""
        return self.db.execute("SELECT * FROM revisions WHERE id = %s AND (%s::text IS NULL OR entry_key = %s)", (revision, key, key)).fetchone()

    def revisions(self, key: str) -> list[dict]:
        """Every revision of an entry, oldest first."""
        return self.db.execute("SELECT * FROM revisions WHERE entry_key = %s ORDER BY seq", (key,)).fetchall()

    def column_revisions(self, table: str, until: int) -> dict[str, list[dict]]:
        """Every revision, up to a point, of every column a table has ever had, by column key, oldest first."""
        out: dict[str, list[dict]] = {}
        for r in self.db.execute("""SELECT r.* FROM revisions r
                                    WHERE r.entry_key IN (SELECT DISTINCT entry_key FROM revisions WHERE body ->> 'kind' = 'column' AND body ->> 'table' = %s)
                                      AND r.seq <= %s ORDER BY r.entry_key, r.seq""", (table, until)):
            out.setdefault(r["entry_key"], []).append(r)
        return out

    def revision_at(self, key: str, when: datetime) -> str | None:
        """The id of the revision of an entry that was its head at a time: the newest recorded at or before it."""
        row = self.db.execute("SELECT id FROM revisions WHERE entry_key = %s AND recorded_at <= %s ORDER BY recorded_at DESC, seq DESC LIMIT 1",
                              (key, when)).fetchone()
        return row["id"] if row else None

    def revision_with(self, key: str, field: str, value: str) -> str | None:
        """The id of the newest revision of an entry whose body says `field` is `value`, such as a page's content digest."""
        row = self.db.execute("SELECT id FROM revisions WHERE entry_key = %s AND body ->> %s = %s ORDER BY recorded_at DESC, seq DESC LIMIT 1",
                              (key, field, value)).fetchone()
        return row["id"] if row else None

    def newest_touching(self, key: str) -> str:
        """The id of the newest revision of an entry or, for a table, of it and its columns: the state a table is in."""
        return self.db.execute("""SELECT id FROM revisions WHERE entry_key = %s OR (body ->> 'kind' = 'column' AND body ->> 'table' = %s)
                                  ORDER BY seq DESC LIMIT 1""", (key, key)).fetchone()["id"]

    # ---- points in the order of all revisions -------------------------------------------------------------------
    def now(self) -> int:
        return self.db.execute("SELECT max(seq) AS seq FROM revisions").fetchone()["seq"] or 0

    def point(self, revision: str) -> int | None:
        """Where a revision, of any entry, sits in the order of all revisions; None when there is no such revision."""
        row = self.db.execute("SELECT seq FROM revisions WHERE id = %s", (revision,)).fetchone()
        return row["seq"] if row else None

    def point_at(self, when: datetime) -> int:
        """The newest point at or before a time; 0 before anything was registered."""
        return self.db.execute("SELECT max(seq) AS seq FROM revisions WHERE recorded_at <= %s", (when,)).fetchone()["seq"] or 0

    # ---- what a revision pinned ---------------------------------------------------------------------------------
    def links(self, revision: str, relation: str) -> list[dict]:
        """What a revision points at by one relation: each target's key, the revision of it that was pinned, and its
        head now (`head_revision`)."""
        return self.db.execute("""SELECT l.target_key, l.target_revision, e.head_revision FROM revision_links l JOIN entries e ON e.key = l.target_key
                                  WHERE l.source_revision = %s AND l.relation = %s ORDER BY l.target_key""", (revision, relation)).fetchall()

    def link_started(self, source: str, relation: str, target: str, target_revision: str) -> str | None:
        """The id of the first revision of `source` that pinned this revision of `target`: when a project began using
        a column as it was then, say. None when no revision of it did."""
        row = self.db.execute("""SELECT r.id FROM revision_links l JOIN revisions r ON r.id = l.source_revision
                                 WHERE r.entry_key = %s AND l.relation = %s AND l.target_key = %s AND l.target_revision = %s
                                 ORDER BY r.seq LIMIT 1""", (source, relation, target, target_revision)).fetchone()
        return row["id"] if row else None
