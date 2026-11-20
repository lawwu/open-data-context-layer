"""Moving from Fathom on SQLite to one Postgres database: the old file comes across once, whole, with its history."""
import hashlib
import sqlite3
from pathlib import Path

import pytest

from odcl import Catalog, CatalogError
from odcl.cli import main

LEGACY = Path(__file__).parent / "fixtures" / "legacy_sqlite" / "catalog.sql"
PLAN_TIER = "column:churn.main_gold.account_features.plan_tier"
# what the fixture's project recorded: it used plan_tier, plan_tier was redefined, then it recorded use again
PROJECT_REVISIONS = ["rev_a7fbde4c19bf4078", "rev_7cf224e226a9414e", "rev_4a6a2d45078a4893"]  # newest first
PLAN_TIER_BEFORE, PLAN_TIER_AFTER = "rev_74c92a389d8044c4", "rev_c70d26d080e0403d"


@pytest.fixture
def sqlite_file(tmp_path):
    path = tmp_path / "fathom.db"
    old = sqlite3.connect(path)
    old.executescript(LEGACY.read_text(encoding="utf-8"))
    old.close()
    return path


def test_a_sqlite_catalog_comes_across_once_with_its_revisions_heads_and_pinned_links(database, sqlite_file, capsys):
    before = hashlib.sha256(sqlite_file.read_bytes()).hexdigest()
    assert main(["--database-url", database, "import-sqlite", str(sqlite_file)]) == 0
    assert "25 entries, 28 revisions" in capsys.readouterr().out
    assert hashlib.sha256(sqlite_file.read_bytes()).hexdigest() == before  # the old file is only read

    with Catalog(database, actor="cutover") as catalog:
        assert catalog.overview()["counts"] == {"table": 5, "column": 19, "project": 1}
        project = catalog.get("project:churn-model")
        assert [h["revision"] for h in project["history"]] == PROJECT_REVISIONS  # the old revision IDs, in the old order
        assert project["registered_by"] == "ana" and project["at"] == "2026-11-20T05:56:37+00:00"
        assert catalog.find("tenure")[0]["key"].endswith("tenure_months")

        now = {u["key"]: u for u in catalog.uses("churn-model")["uses"]}
        assert now[PLAN_TIER]["linked_revision"] == PLAN_TIER_AFTER and not now[PLAN_TIER]["changed_since"]
        # the old links table knew only the line above; what the project had pinned earlier comes from that revision's body
        then = {u["key"]: u for u in catalog.uses("churn-model", revision=PROJECT_REVISIONS[1])["uses"]}
        assert then[PLAN_TIER]["linked_revision"] == PLAN_TIER_BEFORE and then[PLAN_TIER]["changed_since"]
        assert then[PLAN_TIER]["purpose"] == "training features" and len(then) == 2
        with pytest.raises(CatalogError, match="has no revision rev_nope"):
            catalog.uses("churn-model", revision="rev_nope")

        # saying what an imported head already says is still no change
        assert catalog.put("project", {"name": "churn-model", "description": "Predict 90-day churn", "owner": "ben"})["change"] == "unchanged"

    assert main(["--database-url", database, "import-sqlite", str(sqlite_file)]) == 1
    assert "imported once" in capsys.readouterr().err


def test_a_sqlite_catalog_that_does_not_hold_together_imports_nothing(database, sqlite_file):
    old = sqlite3.connect(sqlite_file)
    old.execute("DELETE FROM revisions WHERE id=?", (PLAN_TIER_BEFORE,))  # a revision an older project revision still pins
    old.commit()
    old.close()
    assert main(["--database-url", database, "import-sqlite", str(sqlite_file)]) == 1
    with Catalog(database) as catalog:
        assert catalog.overview()["counts"] == {"table": 0, "column": 0, "project": 0}


def test_a_batch_registers_whole_or_not_at_all(database):
    with Catalog(database, actor="ana") as catalog:
        with pytest.raises(CatalogError):
            catalog.apply([("table", {"path": "a.b"}), ("column", {"table": "a.b", "name": "x"}), ("table", {"path": "Has Spaces"})])
        assert catalog.overview()["counts"] == {"table": 0, "column": 0, "project": 0}
        assert catalog.apply([("table", {"path": "a.b"}), ("column", {"table": "a.b", "name": "x"})]) == {"added": 2, "changed": 0, "unchanged": 0}


def test_the_database_is_named_by_one_url(monkeypatch, capsys):
    monkeypatch.delenv("ODCL_DATABASE_URL", raising=False)
    assert main(["overview"]) == 1 and "ODCL_DATABASE_URL" in capsys.readouterr().err
    assert main(["--database-url", "postgresql://127.0.0.1:1/nowhere?connect_timeout=2", "overview"]) == 1
    assert "Cannot reach the database" in capsys.readouterr().err


def test_the_old_package_name_still_imports():
    from fathom_data import Fathom, FathomError
    from fathom_data.dbt import dbt_entries
    from odcl import dbt
    assert Fathom is Catalog and FathomError is CatalogError and dbt_entries is dbt.dbt_entries
