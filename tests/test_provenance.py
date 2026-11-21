"""What created a table or a file: a script or a notebook, read from git when it is a local file."""
import subprocess

import pytest

from odcl import Catalog, CatalogError
from odcl.cli import main
from odcl.provenance import describe


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "analysis"
    (root / "notebooks").mkdir(parents=True)
    git = lambda *argv: subprocess.run(["git", "-C", str(root), *argv], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "a@example.com")
    git("config", "user.name", "A")
    git("remote", "add", "origin", "git@github.com:example-org/analysis.git")
    (root / "notebooks" / "build_cohort.ipynb").write_text('{"cells": []}')
    (root / "make_features.py").write_text("print(1)\n")
    git("add", ".")
    git("commit", "-qm", "first")
    commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return root, commit


def test_a_local_notebook_or_script_is_described_from_git(repo):
    root, commit = repo
    notebook = describe(str(root / "notebooks" / "build_cohort.ipynb"), note="run by hand each month")
    assert notebook == {"kind": "notebook", "path": "notebooks/build_cohort.ipynb", "repository": "https://github.com/example-org/analysis",
                        "commit": commit, "url": f"https://github.com/example-org/analysis/blob/{commit}/notebooks/build_cohort.ipynb",
                        "note": "run by hand each month"}
    (root / "make_features.py").write_text("print(2)\n")  # edited since its last commit: that commit is not the code that ran
    script = describe(str(root / "make_features.py"), command="python make_features.py --month 2026-10")
    assert script["kind"] == "script" and script["uncommitted"] is True and script["command"] == "python make_features.py --month 2026-10"
    assert describe("https://colab.research.google.com/drive/abc")["kind"] == "other"
    assert describe("jobs/load.sql", repo_url="https://github.com/o/r", commit="abc")["url"] == "https://github.com/o/r/blob/abc/jobs/load.sql"
    with pytest.raises(CatalogError, match="Say where it came from"):
        describe(None, kind="manual")


def test_a_table_says_what_created_it_and_keeps_it_through_imports(database, repo, capsys):
    root, commit = repo
    with Catalog(database, actor="ana") as c:
        c.put("table", {"path": "lake.cohorts.monthly", "system": "gcs", "type": "parquet"})
        made = c.set_created_by("lake.cohorts.monthly", describe(str(root / "notebooks" / "build_cohort.ipynb"), note="monthly cohort"))
        assert made["created_by"]["kind"] == "notebook" and made["created_by"]["commit"] == commit
        c.put("table", {"path": "lake.cohorts.monthly", "description": "Monthly cohort"}, merge=True)
        assert c.get("lake.cohorts.monthly")["created_by"]["path"] == "notebooks/build_cohort.ipynb"  # an import that says nothing of it keeps it
        assert c.find("monthly cohort notebook")[0]["key"] == "table:lake.cohorts.monthly"
        with pytest.raises(CatalogError, match="kind is one of"):
            c.set_created_by("lake.cohorts.monthly", {"kind": "magic", "path": "x"})
        c.set_created_by("lake.cohorts.monthly", None)
        assert "created_by" not in c.get("lake.cohorts.monthly")
    db = ["--database-url", database]
    assert main([*db, "created-by", "lake.cohorts.monthly", str(root / "make_features.py"), "--command", "python make_features.py"]) == 0
    assert "created by script make_features.py at" in capsys.readouterr().out


def test_a_commit_named_for_a_local_file_makes_the_link_too(repo):
    root, commit = repo
    said = describe(str(root / "make_features.py"), commit="abc123")
    assert said["commit"] == "abc123" and said["url"] == "https://github.com/example-org/analysis/blob/abc123/make_features.py"
