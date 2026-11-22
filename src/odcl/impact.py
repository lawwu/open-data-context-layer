"""What each project is exposed to through the data it uses: the one read behind the overview, a project's page
and alerts.

`impact(catalog)` answers one item per project, used entry and kind of exposure:

- `changed`: the entry, or for a table one of its columns, changed after the project used it. `severity` is the
  worst change and `changes` lists them, as `odcl diff` gives them, from when the use was recorded.
- `retired`: the entry is gone from its source. Its severity is breaking.
- `failing`: the newest result of a data-quality test on the entry, or on a column of a used table, is a failure.
  `failing` lists the tests; its severity is breaking.

Each item also has the project's `owner` and a `state`: a mark that moves when the exposure does (the entry's newest
revision, or the newest failing result), so something sent once about a state is not sent again.
"""
from __future__ import annotations

from .changes import diff
from .store import Catalog, CatalogError

KINDS = ("changed", "retired", "failing")


def impact(catalog: Catalog, project: str | None = None, revision: str | None = None, kinds=KINDS) -> list[dict]:
    """Every exposure of every project, or of one. With `revision`, of what that older revision of the project had
    pinned, compared with the data as it is now."""
    db, history = catalog.db, catalog.history
    project = catalog.resolve(project, ("project",)) if project else None
    if revision:
        uses = db.execute("""SELECT sr.entry_key AS project, l.target_key AS key, l.target_revision FROM revision_links l
                             JOIN revisions sr ON sr.id = l.source_revision WHERE l.source_revision = %s AND l.relation = 'uses'
                             ORDER BY l.target_key""", (revision,)).fetchall()
    else:
        uses = db.execute("""SELECT source_key AS project, target_key AS key, target_revision FROM current_links
                             WHERE relation = 'uses' AND (%s::text IS NULL OR source_key = %s) ORDER BY source_key, target_key""", (project, project)).fetchall()
    heads = history.entries({u["key"] for u in uses} | {u["project"] for u in uses})
    out = []
    for u in uses:
        head, owner = heads[u["key"]], heads[u["project"]]["body"].get("owner")
        base = {"project": u["project"], "owner": owner, "key": u["key"]}
        if head["body"].get("retired"):
            if "retired" in kinds:
                out.append({**base, "kind": "retired", "severity": "breaking", "changes": [], "state": history.newest_touching(u["key"])})
        elif "changed" in kinds and (u["target_revision"] != head["id"] or head["body"]["kind"] == "table"):  # a table changes when its columns do
            try:  # from when the use was recorded: a table's columns may have been registered after its own revision
                found = diff(catalog, u["key"], since=history.link_started(u["project"], "uses", u["key"], u["target_revision"]) or u["target_revision"])
            except CatalogError:
                found = {"severity": None, "changes": []}
            if found["severity"] or u["target_revision"] != head["id"]:
                out.append({**base, "kind": "changed", "severity": found["severity"], "changes": found["changes"], "state": history.newest_touching(u["key"])})
    if "failing" in kinds and uses:
        failing: dict[tuple[str, str], list[dict]] = {}
        for r in db.execute("""
                WITH newest AS (SELECT DISTINCT ON (entry_key, source, test) id, entry_key, source, test, status, failures, observed_at FROM quality_results
                                ORDER BY entry_key, source, test, observed_at DESC, id DESC),
                     bad AS (SELECT n.*, r.body ->> 'table' AS tbl FROM newest n JOIN entries e ON e.key = n.entry_key
                             JOIN revisions r ON r.id = e.head_revision WHERE n.status IN ('fail', 'error'))
                SELECT b.* FROM bad b WHERE b.entry_key = ANY(%s) OR b.tbl = ANY(%s) ORDER BY b.entry_key, b.test, b.source""",
                            ([u["key"] for u in uses], [u["key"] for u in uses])):
            for u in uses:
                if u["key"] in (r["entry_key"], r["tbl"]):
                    failing.setdefault((u["project"], u["key"]), []).append(
                        {"id": r["id"], "entry": r["entry_key"], "test": r["test"], "status": r["status"], "failures": r["failures"], "source": r["source"]})
        for (who, key), tests in sorted(failing.items()):
            out.append({"project": who, "owner": heads[who]["body"].get("owner"), "key": key, "kind": "failing", "severity": "breaking", "changes": [],
                        "failing": [{k: v for k, v in t.items() if k != "id"} for t in tests], "state": f"quality:{max(t['id'] for t in tests)}"})
    return sorted(out, key=lambda i: (i["project"], i["key"], KINDS.index(i["kind"])))
