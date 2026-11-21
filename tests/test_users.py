"""Users, roles and API tokens: in Python, on the command line, over HTTP and through the MCP tools."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from odcl import Catalog, users
from odcl.cli import main
from odcl.mcp import Remote, Tools
from odcl.server import make_server
from odcl.store import CatalogError
from test_openlineage import EVENT

TABLE = {"path": "core.sales.daily_totals", "description": "Sales per day", "system": "postgres"}


@pytest.fixture
def people(database):
    """An admin made over a direct connection (how the first one is made), then one user of each role, each with a token."""
    with Catalog(database, actor="setup") as direct:
        direct.put("table", TABLE)
        made = {}
        for name, role in (("ana", "admin"), ("ben", "editor"), ("cam", "reader"), ("etl", "importer")):
            users.add(direct, name, role, display_name=name.title())
            made[name] = users.make_token(direct, name, "test")["token"]
    return made


@pytest.fixture
def serve(database):
    servers = []

    def start(**options):
        server = make_server(database, host="127.0.0.1", port=0, sso=options.pop("sso", False), **options)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"
    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def call(url, method="GET", body=None, token=None, headers=None):
    request = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {}), **(headers or {})})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


# ---- in Python -------------------------------------------------------------------------------------------------
def test_a_token_signs_in_and_writes_are_recorded_under_its_user(database, people):
    with Catalog(database, actor="someone-else", token=people["ben"]) as c:
        assert c.user["name"] == "ben" and c.actor == "ben"
        written = c.put("project", {"name": "weekly_report", "description": "A weekly sales report"})
        assert written["registered_by"] == "ben"
        c.use("weekly_report", ["core.sales.daily_totals"], "the totals")
        assert c.get("project:weekly_report")["history"][0]["registered_by"] == "ben"


def test_roles_decide_what_a_user_may_do(database, people):
    with Catalog(database, token=people["cam"]) as reader:
        assert reader.find("sales")[0]["key"] == "table:core.sales.daily_totals"
        with pytest.raises(CatalogError) as refused:
            reader.put("project", {"name": "p", "description": "d"})
        assert refused.value.code == "forbidden" and "cam is a reader" in str(refused.value)
    with Catalog(database, token=people["ben"]) as editor:
        editor.set_privacy(["core.sales.daily_totals"], pii=False)
        with pytest.raises(CatalogError, match="needs importer"):
            editor.apply([("table", {**TABLE, "description": "Changed"})])
        with pytest.raises(CatalogError, match="needs importer"):
            from odcl import openlineage
            openlineage.ingest(editor, EVENT)
        with pytest.raises(CatalogError, match="needs admin"):
            users.listing(editor)
    with Catalog(database, token=people["etl"]) as importer:
        assert importer.apply([("table", {**TABLE, "description": "Changed"})])["changed"] == 1
        assert importer.get("core.sales.daily_totals")["registered_by"] == "etl"
    with Catalog(database, token=people["ana"]) as admin:
        assert [u["name"] for u in users.listing(admin)] == ["ana", "ben", "cam", "etl"]
        with pytest.raises(CatalogError, match="another admin"):
            users.update(admin, "ana", role="reader")


def test_a_direct_connection_is_trusted_and_records_its_actor(database, people):
    with Catalog(database, actor="nightly") as direct:
        assert direct.user is None
        assert users.whoami(direct) == {"name": "nightly", "role": None, "direct": True}
        assert direct.apply([("table", {**TABLE, "description": "Direct"})])["changed"] == 1
        assert len(users.listing(direct)) == 4


def test_tokens_stop_working_when_revoked_or_their_user_is_disabled(database, people):
    with Catalog(database, token=people["ben"]) as ben:
        second = users.make_token(ben, label="laptop")
        assert [t["label"] for t in users.tokens(ben)] == ["test", "laptop"]
        users.revoke(ben, second["id"])
        with pytest.raises(CatalogError, match="needs admin"):
            users.make_token(ben, "cam")
    with pytest.raises(CatalogError, match="not a current"):
        Catalog(database, token=second["token"])
    with pytest.raises(CatalogError, match="not an odcl API token"):
        Catalog(database, token="nonsense")
    with Catalog(database, token=people["ana"]) as admin:
        assert users.update(admin, "ben", disabled=True)["disabled"] is True
    with pytest.raises(CatalogError, match="ben is disabled"):
        Catalog(database, token=people["ben"])
    with Catalog(database, token=people["ana"]) as admin:
        users.update(admin, "ben", disabled=False, role="importer")
    with Catalog(database, token=people["ben"]) as ben:
        assert ben.user["role"] == "importer"


def test_user_names_and_roles_are_checked(database):
    with Catalog(database) as direct:
        with pytest.raises(CatalogError, match="role is one of"):
            users.add(direct, "dee", "owner")
        with pytest.raises(CatalogError, match="user name"):
            users.add(direct, "Dee Smith!", "reader")
        users.add(direct, "dee", "reader")
        with pytest.raises(CatalogError, match="already a user"):
            users.add(direct, "DEE", "editor")


# ---- on the command line -----------------------------------------------------------------------------------------
def test_the_command_line_manages_users_and_signs_in_with_odcl_token(database, monkeypatch, capsys):
    monkeypatch.delenv("ODCL_TOKEN", raising=False)
    assert main(["--database-url", database, "--as", "root", "users", "add", "ana", "--role", "admin", "--email", "Ana@Example.com"]) == 0
    assert main(["--database-url", database, "--json", "users", "token", "ana", "--label", "laptop"]) == 0
    token = json.loads(capsys.readouterr().out.split("\n", 1)[1])["token"]
    monkeypatch.setenv("ODCL_TOKEN", token)
    assert main(["--database-url", database, "users", "whoami"]) == 0
    assert capsys.readouterr().out.startswith("ana  admin  <ana@example.com>")
    assert main(["--database-url", database, "users", "add", "ben", "--role", "reader"]) == 0
    assert main(["--database-url", database, "users", "list"]) == 0
    listed = capsys.readouterr().out
    assert "ana  admin" in listed and "ben  reader" in listed and "1 token" in listed
    monkeypatch.setenv("ODCL_TOKEN", "odcl_wrong")
    assert main(["--database-url", database, "users", "whoami"]) == 1
    assert "not a current odcl API token" in capsys.readouterr().err


# ---- over HTTP -------------------------------------------------------------------------------------------------
def test_reads_are_open_and_writes_need_an_editor(people, serve):
    url = serve()
    assert call(f"{url}/v1/search?q=sales")[0] == 200
    assert call(f"{url}/v1/me") == (200, {"user": None})
    project = {"name": "weekly_report", "description": "A weekly sales report"}
    status, body = call(f"{url}/v1/projects", "POST", project)
    assert status == 401 and body["error"]["code"] == "unauthorized" and "sign_in" not in body["error"]
    assert call(f"{url}/v1/projects", "POST", project, token=people["cam"])[0] == 403
    assert call(f"{url}/v1/projects", "POST", project, token="odcl_wrong")[0] == 401
    status, body = call(f"{url}/v1/projects", "POST", project, token=people["ben"])
    assert status == 200 and body["registered_by"] == "ben" and body["change"] == "added"
    status, body = call(f"{url}/v1/projects/weekly_report/uses", "POST", {"keys": ["core.sales.daily_totals"], "purpose": "totals"}, token=people["ben"])
    assert status == 200 and body["links"][0]["to"] == "table:core.sales.daily_totals"
    status, body = call(f"{url}/v1/privacy", "POST", {"keys": ["core.sales.daily_totals"], "pii": True, "data_category": "financial"}, token=people["ben"])
    assert status == 200 and body["changed"][0]["data_category"] == "financial"
    assert call(f"{url}/v1/privacy", "POST", {"keys": ["core.sales.daily_totals"], "colour": "red"}, token=people["ben"])[0] == 400
    assert call(f"{url}/v1/entries/table%3Acore.sales.daily_totals/created-by", "POST",
                {"created_by": {"kind": "query", "command": "CREATE TABLE ..."}}, token=people["ben"])[0] == 200
    assert call(f"{url}/v1/projects", "GET")[0] == 200
    assert call(f"{url}/v1/overview", "POST", {})[0] == 405


def test_require_sign_in_closes_reads_too(people, serve):
    url = serve(require_sign_in=True)
    assert call(f"{url}/v1/search?q=sales")[0] == 401
    assert call(f"{url}/v1/search?q=sales", token=people["cam"])[0] == 200
    assert call(f"{url}/v1/me", token=people["cam"])[1]["user"]["name"] == "cam"
    with urllib.request.urlopen(f"{url}/") as page:  # the page loads, and asks for a token when the API answers 401
        assert page.status == 200


def test_users_and_tokens_over_http(people, serve):
    url = serve()
    assert call(f"{url}/v1/users", token=people["ben"])[0] == 403
    status, made = call(f"{url}/v1/users", "POST", {"name": "ci", "role": "importer", "display_name": "CI"}, token=people["ana"])
    assert status == 200 and made["role"] == "importer"
    assert call(f"{url}/v1/users", "POST", {"name": "ci", "role": "importer"}, token=people["ana"])[0] == 409
    status, token = call(f"{url}/v1/users/ci/tokens", "POST", {"label": "nightly"}, token=people["ana"])
    assert status == 200 and token["token"].startswith("odcl_")
    assert call(f"{url}/v1/me", token=token["token"])[1]["user"]["name"] == "ci"
    assert call(f"{url}/v1/users/ben", "POST", {"role": "reader"}, token=people["ana"])[1]["role"] == "reader"
    status, mine = call(f"{url}/v1/me/tokens", "POST", {"label": "laptop"}, token=people["cam"])
    assert status == 200 and mine["user"] == "cam"
    assert [t["label"] for t in call(f"{url}/v1/me/tokens", token=people["cam"])[1]] == ["test", "laptop"]
    assert call(f"{url}/v1/me/tokens/{mine['id']}/revoke", "POST", token=people["cam"])[1]["revoked"] is True
    assert call(f"{url}/v1/me", token=mine["token"])[0] == 401
    assert call(f"{url}/v1/me/tokens", "POST", {})[0] == 401


def test_openlineage_events_take_an_importer_token_or_the_shared_one(people, serve, monkeypatch):
    url = serve(accept_lineage=True)
    assert call(f"{url}/api/v1/lineage", "POST", EVENT, token=people["ben"])[0] == 403
    status, body = call(f"{url}/api/v1/lineage", "POST", EVENT, token=people["etl"])
    assert status == 201, body
    monkeypatch.setenv("ODCL_LINEAGE_TOKEN", "shared-secret")
    assert call(f"{url}/api/v1/lineage", "POST", {**EVENT, "eventTime": "2026-01-01T00:00:00Z"})[0] == 401
    assert call(f"{url}/api/v1/lineage", "POST", {**EVENT, "eventType": "COMPLETE"}, token="shared-secret")[0] in (200, 201)


# ---- through the MCP tools ---------------------------------------------------------------------------------------
def test_mcp_tools_follow_the_users_role(database, people):
    with Catalog(database, token=people["cam"]) as reader:
        tools = Tools(reader, allow_writes=True)
        assert "register_project" not in [t["name"] for t in tools.tools()]
        result, failed = tools.result("register_project", {"name": "p", "description": "d"})
        assert failed and "cam is a reader" in result["error"]
    with Catalog(database, token=people["ben"]) as editor:
        assert "register_project" in [t["name"] for t in Tools(editor, allow_writes=True).tools()]


def test_mcp_through_a_server_needs_no_database_credentials(people, serve):
    url = serve()
    remote = Tools(Remote(url, people["ben"]), allow_writes=True)
    found, failed = remote.result("find_data", {"query": "sales"})
    assert not failed and found["results"][0]["key"] == "table:core.sales.daily_totals"
    made, failed = remote.result("register_project", {"name": "weekly_report", "description": "A weekly sales report"})
    assert not failed and made["registered_by"] == "ben"
    used, failed = remote.result("record_use", {"project": "weekly_report", "keys": ["core.sales.daily_totals"]})
    assert not failed and used["links"][0]["to"] == "table:core.sales.daily_totals"
    entry, failed = remote.result("get_entry", {"key": "core.sales.daily_totals"})
    assert not failed and entry["key"] == "table:core.sales.daily_totals"
    assert not remote.result("trace_lineage", {"key": "core.sales.daily_totals"})[1]
    assert remote.result("used_by", {"key": "core.sales.daily_totals"})[0]["projects"][0]["project"] == "project:weekly_report"
    assert not remote.result("project_uses", {"project": "weekly_report", "upstream": True})[1]
    assert not remote.result("documents_for", {"key": "core.sales.daily_totals"})[1]
    anonymous = Tools(Remote(url, None), allow_writes=True)
    assert "register_project" not in [t["name"] for t in anonymous.tools()]
    with pytest.raises(CatalogError, match="https"):
        Remote("http://catalog.example.com", people["ben"])
