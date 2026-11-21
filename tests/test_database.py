"""The catalog's database: one PostgreSQL database named by one URL, written a whole batch at a time."""
import pytest

from odcl import Catalog, CatalogError
from odcl.cli import main


def test_a_batch_registers_whole_or_not_at_all(database):
    with Catalog(database, actor="ana") as catalog:
        with pytest.raises(CatalogError):
            catalog.apply([("table", {"path": "a.b"}), ("column", {"table": "a.b", "name": "x"}), ("table", {"path": "Has Spaces"})])
        assert catalog.overview()["counts"] == {"table": 0, "column": 0, "project": 0, "document": 0}
        assert catalog.apply([("table", {"path": "a.b"}), ("column", {"table": "a.b", "name": "x"})]) == {"added": 2, "changed": 0, "unchanged": 0}


def test_the_database_is_named_by_one_url(monkeypatch, capsys):
    monkeypatch.delenv("ODCL_DATABASE_URL", raising=False)
    assert main(["overview"]) == 1 and "ODCL_DATABASE_URL" in capsys.readouterr().err
    assert main(["--database-url", "postgresql://127.0.0.1:1/nowhere?connect_timeout=2", "overview"]) == 1
    assert "Cannot reach the database" in capsys.readouterr().err
