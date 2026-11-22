"""Documents: Markdown pages that describe catalog entries, linked to them and versioned like every other entry."""
import shutil
from pathlib import Path

import pytest

from odcl import Catalog, docs

pytest.importorskip("yaml")

FIXTURE = Path(__file__).parent / "fixtures" / "docs"
REPO = "https://github.com/example-org/knowledge-base"
COMMIT = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
PAGE = "document:example-org/knowledge-base/docs/reference/order-status.md"
GLOSSARY = "document:example-org/knowledge-base/docs/glossary/amount.md"
STATUS = "column:warehouse.sales.orders.status_code"


@pytest.fixture
def kb(tmp_path):
    root = tmp_path / "knowledge-base"
    shutil.copytree(FIXTURE, root)
    return root


@pytest.fixture
def catalog(database):
    with Catalog(database, actor="ana") as c:
        for table, columns in (("warehouse.sales.orders", ["status_code", "amount"]), ("warehouse.ref.status_lookup", ["code"]),
                               *[(f"{region}-prod.sales.orders", ["amount", "amount_usd"]) for region in ("eu", "us", "ap")]):
            c.put("table", {"path": table, "description": f"{table} as loaded", "system": "bigquery"})
            for column in columns:
                c.put("column", {"table": table, "name": column, "description": column.replace("_", " ")})
        yield c


def load(catalog, kb, *paths, **options):
    return docs.import_docs(catalog, [str(p) for p in paths] or [str(kb)], repo_url=REPO, commit=COMMIT, **options)


def test_a_page_describes_entries_by_key_with_a_section_and_what_it_says(catalog, kb):
    result = load(catalog, kb)
    assert result["documents"] == {"added": 2, "changed": 0, "unchanged": 0, "retired": 0} and result["skipped"] == 2
    page = catalog.get(PAGE)
    assert page["kind"] == "document" and page["title"] == "Order status codes" and page["owner"] == "Data Team" and page["doc_kind"] == "reference"
    assert page["commit"] == COMMIT and page["url"] == f"{REPO}/blob/{COMMIT}/docs/reference/order-status.md"
    assert [h["anchor"] for h in page["headings"]] == ["order-status-codes", "status-codes", "refund-handling"]  # a # inside code is not a heading
    described = {d["key"]: d for d in page["describes"]}
    assert described[STATUS]["section"] == "status-codes" and described[STATUS]["says"] == "Closed = status_code IN ('C', 'X')"
    assert set(described) == {STATUS, "table:warehouse.ref.status_lookup", "table:warehouse.sales.orders"}

    [documented] = catalog.get(STATUS)["documents"]
    assert documented | {} == {"document": PAGE, "title": "Order status codes", "doc_kind": "reference", "section": "status-codes",
                               "says": "Closed = status_code IN ('C', 'X')", "through": STATUS, "changed_since": False}
    # a column also shows the documents that describe its table
    through_table = [d for d in catalog.get("column:warehouse.sales.orders.amount")["documents"] if d["document"] == PAGE]
    assert through_table[0]["through"] == "table:warehouse.sales.orders"


def test_a_key_the_catalog_lacks_or_a_section_the_page_lacks_is_reported_not_refused(catalog, kb):
    result = load(catalog, kb)
    assert result["unresolved"][PAGE] == ["column:warehouse.sales.orders.legacy_flag", "section shipping-windows of table:warehouse.sales.orders"]
    assert catalog.get(PAGE)["unresolved"] == result["unresolved"][PAGE]


def test_a_pattern_links_every_copy_of_a_column_and_a_later_import_picks_up_new_ones(catalog, kb):
    load(catalog, kb)
    linked = sorted(d["key"] for d in catalog.get(GLOSSARY)["describes"])
    assert linked == [f"column:{r}-prod.sales.orders.{c}" for r in ("ap", "eu", "us") for c in ("amount", "amount_usd")]
    assert catalog.get(GLOSSARY)["patterns"] == ["column:*-prod.sales.orders.amount*"]
    catalog.put("table", {"path": "sa-prod.sales.orders", "description": "South America", "system": "bigquery"})
    catalog.put("column", {"table": "sa-prod.sales.orders", "name": "amount"})
    assert load(catalog, kb)["documents"]["changed"] == 1
    assert "column:sa-prod.sales.orders.amount" in {d["key"] for d in catalog.get(GLOSSARY)["describes"]}


def test_importing_again_changes_nothing_and_an_edit_is_one_new_revision(catalog, kb):
    load(catalog, kb)
    assert load(catalog, kb)["documents"] == {"added": 0, "changed": 0, "unchanged": 2, "retired": 0}
    page = kb / "docs" / "reference" / "order-status.md"
    page.write_text(page.read_text().replace("one code per order", "one code per order line"))
    assert load(catalog, kb)["documents"] == {"added": 0, "changed": 1, "unchanged": 1, "retired": 0}
    assert len(catalog.get(PAGE)["history"]) == 2


def test_a_page_removed_is_retired_and_its_links_stay_as_history(catalog, kb):
    load(catalog, kb)
    (kb / "docs" / "reference" / "order-status.md").unlink()
    assert load(catalog, kb)["documents"]["retired"] == 1
    page = catalog.get(PAGE)
    assert page["retired"] is True and page["describes"] == [] and len(page["history"]) == 2
    assert catalog.get(STATUS)["documents"] == []  # not current any more
    assert load(catalog, kb)["documents"]["retired"] == 0  # retired once
    # a live page keeps "retired": false, and the one SQL reading of retired reads the value, not whether the key is there
    from odcl.history import retired_sql
    rows = catalog.db.execute(f"""SELECT e.key, {retired_sql()} AS retired FROM entries e JOIN revisions r ON r.id = e.head_revision
                                  WHERE e.kind = 'document' ORDER BY e.key""").fetchall()
    assert {r["key"]: r["retired"] for r in rows} == {PAGE: True, **{r["key"]: False for r in rows if r["key"] != PAGE}} and len(rows) == 2
    assert all("retired" in catalog.history.entry(r["key"])["body"] for r in rows)
    # importing one file does not retire the pages it was not asked about
    assert load(catalog, kb, kb / "docs" / "glossary" / "amount.md")["documents"]["retired"] == 0


def test_a_column_redefined_after_the_page_described_it_is_reported(catalog, kb):
    load(catalog, kb)
    catalog.put("column", {"table": "warehouse.sales.orders", "name": "status_code", "description": "Now one of O, C, X, R"})
    assert catalog.get(STATUS)["documents"][0]["changed_since"] is True
    assert {d["key"]: d["changed_since"] for d in catalog.get(PAGE)["describes"]}[STATUS] is True
    assert {"document": PAGE, "key": STATUS} in catalog.overview()["changed_since_described"]
    assert load(catalog, kb)["documents"]["unchanged"] == 2  # importing the same page again does not mean someone re-read it


def test_search_finds_a_page_by_a_word_in_its_body_or_headings(catalog, kb):
    load(catalog, kb)
    assert [r["key"] for r in catalog.find("refund reopens")] == [PAGE]
    assert PAGE in [r["key"] for r in catalog.find("order status", kind="document")]


def test_git_supplies_the_commit_and_web_address(catalog, kb):
    import subprocess
    git = lambda *a: subprocess.run(["git", "-C", str(kb), *a], check=True, capture_output=True, text=True)  # noqa: E731
    git("init", "-q")
    git("remote", "add", "origin", "git@github.com:example-org/knowledge-base.git")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "pages")
    head = git("rev-parse", "HEAD").stdout.strip()
    docs.import_docs(catalog, [str(kb / "docs")])
    page = catalog.get(PAGE)
    assert page["commit"] == head and page["url"] == f"{REPO}/blob/{head}/docs/reference/order-status.md"


def test_the_command_line_imports_and_lists_the_documents_of_an_entry(database, kb, capsys):
    from odcl.cli import main
    with Catalog(database) as c:
        c.put("table", {"path": "warehouse.sales.orders", "description": "Orders"})
        c.put("column", {"table": "warehouse.sales.orders", "name": "status_code"})
    db = ["--database-url", database]
    assert main([*db, "import", "--docs", str(kb), "--repo-url", REPO, "--commit", COMMIT]) == 0
    out = capsys.readouterr().out
    assert "2 documents: 2 added, 0 changed, 0 unchanged, 0 retired; 2 files without an odcl block skipped" in out
    assert "names 2 entries the catalog does not have" in out
    assert main([*db, "docs", "warehouse.sales.orders.status_code"]) == 0
    assert f"{PAGE}  #status-codes  Closed = status_code IN ('C', 'X')" in capsys.readouterr().out


def test_the_web_route_and_the_mcp_tool_answer_what_the_python_read_answers(database, catalog, kb):
    pytest.importorskip("mcp")
    import json
    import threading
    import urllib.request
    from urllib.parse import quote

    from odcl.mcp import Tools
    from odcl.server import make_server
    load(catalog, kb)
    expected = catalog.documents(STATUS)
    assert Tools(catalog).call("documents_for", {"key": STATUS}) == {"documents": expected}
    assert PAGE in [r["key"] for r in Tools(catalog).call("find_data", {"query": "refund reopens"})["results"]]
    server = make_server(database, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}/v1/entries"
        with urllib.request.urlopen(f"{base}/{quote(STATUS, safe='')}/documents") as response:
            assert json.loads(response.read()) == expected
        with urllib.request.urlopen(f"{base}/{quote(PAGE, safe='')}") as response:
            assert json.loads(response.read()) == json.loads(json.dumps(catalog.get(PAGE), default=str))
    finally:
        server.shutdown()
        server.server_close()


def test_a_page_whose_frontmatter_a_linter_owns_puts_the_odcl_block_in_an_html_comment(catalog, tmp_path):
    page = tmp_path / "kb" / "finding.md"
    page.parent.mkdir()
    page.write_text("---\ntitle: Refunds are booked late\nowner: Finance\n---\n# Refunds are booked late\n\n"
                    "Refunds land a day after the order closes.\n\n<!-- odcl\nkind: finding\ndescribes:\n"
                    "  - key: column:warehouse.sales.orders.amount\n    says: refunds lag by a day\n-->\n\n## Why\n\nBatch timing.\n")
    assert docs.import_docs(catalog, [str(page.parent)], repo_url=REPO, commit=COMMIT)["documents"]["added"] == 1
    found = catalog.get("document:example-org/knowledge-base/finding.md")
    assert found["doc_kind"] == "finding" and found["owner"] == "Finance" and found["title"] == "Refunds are booked late"
    assert [(d["key"], d["says"]) for d in found["describes"]] == [("column:warehouse.sales.orders.amount", "refunds lag by a day")]
    assert "odcl" not in found["text"] and "Batch timing." in found["text"]  # the comment is not part of the page's text
    page.write_text("# No frontmatter at all\n\n<!-- odcl:\ndescribes:\n  - key: table:warehouse.sales.orders\n-->\n")
    docs.import_docs(catalog, [str(page)], repo_url=REPO, commit=COMMIT)
    assert [d["key"] for d in catalog.get("document:example-org/knowledge-base/finding.md")["describes"]] == ["table:warehouse.sales.orders"]


def test_an_odcl_comment_shown_as_an_example_in_a_code_block_is_not_the_page_s_block():
    body = "# How to tag a page\n\n```markdown\n<!-- odcl\nkind: finding\n-->\n```\n\nThat is all.\n"
    block, kept = docs.odcl_block(None, body)
    assert block is None and kept == body
    block, kept = docs.odcl_block(None, body + "\n<!-- odcl\nkind: guide\n-->\n")
    assert block == {"kind": "guide"} and "kind: finding" in kept
