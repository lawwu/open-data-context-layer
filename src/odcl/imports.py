"""One import, whatever it reads from: what an importer registers, what it could not link, and what is now gone.

    with catalog.importing("postgres", scope={"database": "shop", "schema": ["public"]}) as imp:
        imp.extend(entries)                       # (kind, payload) pairs, dependencies first
        imp.unresolved("column:a.b.c.x -> column:a.b.d.id")
        imp.skip("a.b.Odd Name: not a name the catalog keys")
    imp.result

Leaving the block applies everything in one transaction: all of it, or none. `scope` says what the import read, in
the importer's own words as it keeps them in each table's `source` (its database and schemas, its repository, its
dbt project); a table the importer said before, within that scope, and did not say this time is withdrawn, and so is
each column of such a table it no longer says (see `Catalog.withdraw`). A value in `scope` may be a list: any of
them. `tables` names the tables read instead, for an import that reads only those; then only their columns are
withdrawn, never a table. With neither, nothing is withdrawn.

Every import's result has the same seven fields: `added`, `changed` and `unchanged` (counts of entries), `retired`
and `withdrawn` (keys), `unresolved` (what the source named that the catalog does not have, so no link was made)
and `skipped` (what was read and could not be registered, with why). An importer adds its own beside them.
"""
from __future__ import annotations

FIELDS = ("added", "changed", "unchanged", "retired", "withdrawn", "unresolved", "skipped")


def empty(**extra) -> dict:
    """The result of an import that registered nothing."""
    return {"added": 0, "changed": 0, "unchanged": 0, "retired": [], "withdrawn": [], "unresolved": [], "skipped": [], **extra}


def _in_scope(scope: dict):
    def check(said: dict, body: dict) -> bool:
        return all(said.get(field) in wanted if isinstance(wanted, (list, tuple, set)) else said.get(field) == wanted
                   for field, wanted in scope.items() if wanted is not None)
    return check


class ImportSession:
    def __init__(self, catalog, importer: str, *, scope: dict | None = None, tables=None, merge: bool = True):
        self.catalog, self.importer, self.merge = catalog, importer, merge
        self.scope, self.tables = scope, ({t if t.startswith("table:") else f"table:{t}" for t in (x.lower() for x in tables)} if tables is not None else None)
        self.entries: list[tuple[str, dict]] = []
        self._unresolved: list[str] = []
        self._skipped: list[str] = []
        self.result: dict | None = None

    def add(self, kind: str, payload: dict) -> None:
        self.entries.append((kind, payload))

    def extend(self, entries) -> None:
        self.entries.extend(entries)

    def unresolved(self, *what: str) -> None:
        """Something the source named that the catalog does not have, so it was not linked."""
        self._unresolved.extend(what)

    def skip(self, *what: str) -> None:
        """Something read that could not be registered, and why."""
        self._skipped.extend(what)

    def __enter__(self) -> ImportSession:
        return self

    def __exit__(self, kind, error, traceback) -> None:
        if kind is not None:
            return
        withdraw = None
        if self.tables is not None:
            withdraw = {"importer": self.importer, "scope": lambda _, body: body["key"] in self.tables, "tables": False}
        elif self.scope is not None:
            withdraw = {"importer": self.importer, "scope": _in_scope(self.scope)}
        counts = self.catalog.apply(self.entries, merge=self.merge, withdraw=withdraw)
        self.result = empty() | counts | {"unresolved": sorted(set(self._unresolved)), "skipped": list(self._skipped)}
