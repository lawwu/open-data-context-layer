"""How much tables are used, from the synced query-job history."""
from datetime import timedelta

import pytest

from odcl import Catalog, popularity
from odcl.cli import main
from odcl.store import now


def job(c, job_id, reads, days_ago=1, parent=None, pattern=None):
    c.db.execute("INSERT INTO query_jobs (execution_project, location, job_id, parent_job_id, created_at, state, pattern_id) "
                 "VALUES ('p', 'us', %s, %s, %s, 'DONE', %s)", (job_id, parent, now() - timedelta(days=days_ago), pattern))
    for t in reads:
        c.db.execute("INSERT INTO query_tables (execution_project, location, job_id, table_key, role, evidence) VALUES ('p', 'us', %s, %s, 'read', 'test')",
                     (job_id, t))


@pytest.fixture
def catalog(database):
    with Catalog(database, actor="ana") as c:
        for t in ("core.sales.daily_totals", "core.sales.stores", "core.sales.quiet"):
            c.put("table", {"path": t, "description": "Sales data"})
        c.put("column", {"table": "core.sales.daily_totals", "name": "amount", "description": "Sales amount"})
        c.put("column", {"table": "core.sales.quiet", "name": "amount", "description": "Sales amount"})
        patterns = [c.db.execute("INSERT INTO query_patterns (scope, template_digest, template) VALUES ('p', %s, 'SELECT ?') RETURNING id", (d,)).fetchone()["id"]
                    for d in ("a", "b")]
        job(c, "j1", ["Core.Sales.Daily_Totals", "core.sales.stores"], pattern=patterns[0])  # BigQuery's spelling
        job(c, "j2", ["core.sales.daily_totals", "core.sales.stores"], pattern=patterns[0])
        job(c, "j3", ["core.sales.daily_totals", "other.raw.events"], pattern=patterns[1])
        job(c, "script", [], pattern=None)
        job(c, "script_0", ["core.sales.daily_totals"], parent="script")  # a statement in a script and the script count once
        job(c, "script_1", ["core.sales.daily_totals"], parent="script")
        job(c, "old", ["core.sales.quiet"], days_ago=90)
        yield c


def test_reads_count_top_level_jobs_in_the_window(catalog):
    used = popularity.reads(catalog, ["table:core.sales.daily_totals", "table:core.sales.quiet"])
    assert used["table:core.sales.daily_totals"]["reads"] == 4 and used["table:core.sales.daily_totals"]["shapes"] == 2
    assert used["table:core.sales.quiet"] == {"reads": 0, "shapes": 0, "last_read": None}
    assert popularity.reads(catalog, ["table:core.sales.quiet"], since=timedelta(days=120))["table:core.sales.quiet"]["reads"] == 1


def test_tables_read_together_and_the_most_read(catalog):
    assert popularity.read_with(catalog, "core.sales.daily_totals") == [
        {"table": "core.sales.stores", "key": "table:core.sales.stores", "jobs": 2}, {"table": "other.raw.events", "key": None, "jobs": 1}]
    top = popularity.popular(catalog)
    assert [(t["table"], t["reads"]) for t in top[:2]] == [("core.sales.daily_totals", 4), ("core.sales.stores", 2)]


def test_lists_and_search_put_the_most_read_first(catalog, database, capsys):
    assert [t["key"] for t in catalog.tables(sort="reads")][:2] == ["table:core.sales.daily_totals", "table:core.sales.stores"]
    assert catalog.tables()[0]["key"] == "table:core.sales.daily_totals" and catalog.tables()[0]["reads"] == 4
    assert [r["key"] for r in catalog.find("sales amount", kind="column")] == ["column:core.sales.daily_totals.amount", "column:core.sales.quiet.amount"]
    usage = catalog.get("core.sales.daily_totals")["usage"]
    assert usage["reads"] == 4 and usage["read_with"][0]["table"] == "core.sales.stores"
    assert main(["--database-url", database, "popular", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "core.sales.daily_totals  4 reads" in out
