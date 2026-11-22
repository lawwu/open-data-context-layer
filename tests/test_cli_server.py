"""The command line through an `odcl serve`: no database credentials, and writes recorded as the token's user."""
import threading

import pytest

from odcl import Catalog, users
from odcl.cli import main
from odcl.server import make_server


@pytest.fixture
def server(database, monkeypatch):
    with Catalog(database, actor="setup") as c:
        c.put("table", {"path": "core.sales.daily_totals", "description": "Totals by day"})
        c.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "NUMERIC"})
        c.put("project", {"name": "forecast", "description": "Daily forecast"})
        users.add(c, "root", "admin")
        users.add(c, "ana", "editor")
        users.add(c, "rex", "reader")
        tokens = {name: users.make_token(c, name)["token"] for name in ("root", "ana", "rex")}
    http = make_server(database, host="127.0.0.1", port=0)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    monkeypatch.delenv("ODCL_DATABASE_URL", raising=False)  # nothing here can reach the database
    yield f"http://127.0.0.1:{http.server_address[1]}", tokens, database
    http.shutdown()
    http.server_close()


def odcl(capsys, monkeypatch, url, token, *argv):
    monkeypatch.setenv("ODCL_TOKEN", token) if token else monkeypatch.delenv("ODCL_TOKEN", raising=False)
    code = main(["--server", url, *argv])
    out = capsys.readouterr()
    return code, out.out + out.err


def test_reads_and_writes_go_through_the_server_as_the_token_s_user(server, capsys, monkeypatch):
    url, tokens, database = server
    code, out = odcl(capsys, monkeypatch, url, None, "find", "totals")
    assert code == 0 and "table:core.sales.daily_totals" in out
    assert odcl(capsys, monkeypatch, url, tokens["ana"], "use", "forecast", "core.sales.daily_totals.amount", "--for", "a feature")[0] == 0
    code, out = odcl(capsys, monkeypatch, url, tokens["ana"], "uses", "forecast")
    assert code == 0 and "column:core.sales.daily_totals.amount  for: a feature" in out
    with Catalog(database) as c:
        assert c.get("forecast")["registered_by"] == "ana"  # recorded as the token's user
    code, out = odcl(capsys, monkeypatch, url, tokens["rex"], "use", "forecast", "core.sales.daily_totals.amount")
    assert code == 1 and "reader" in out  # the server checks the role
    code, out = odcl(capsys, monkeypatch, url, None, "use", "forecast", "core.sales.daily_totals.amount")
    assert code == 1 and "Sign in" in out
    monkeypatch.setenv("ODCL_SERVER", url)  # the server can also come from the environment
    monkeypatch.setenv("ODCL_TOKEN", tokens["ana"])
    assert main(["users", "whoami"]) == 0 and "ana  editor" in capsys.readouterr().out


def test_what_needs_working_out_here_is_worked_out_before_it_is_sent(server, capsys, monkeypatch):
    url, tokens, database = server
    assert odcl(capsys, monkeypatch, url, tokens["ana"], "created-by", "core.sales.daily_totals", "https://example.com/notebook.ipynb")[0] == 0
    assert odcl(capsys, monkeypatch, url, tokens["ana"], "alerts", "subscribe", "--project", "forecast", "--slack", "https://hooks.example.com/x")[0] == 0
    code, out = odcl(capsys, monkeypatch, url, tokens["root"], "users", "token", "rex", "--label", "ci")  # another user's: the admin route
    assert code == 0 and "A token for rex" in out
    with Catalog(database) as c:
        assert c.get("core.sales.daily_totals")["created_by"]["kind"] == "notebook"
    code, out = odcl(capsys, monkeypatch, url, tokens["ana"], "alerts", "list")
    assert code == 0 and "-> slack https://hooks.example.com/…" in out


def test_what_reads_from_this_machine_says_it_needs_the_database(server, capsys, monkeypatch, tmp_path):
    url, tokens, _ = server
    code, out = odcl(capsys, monkeypatch, url, tokens["root"], "import", "--dbt", str(tmp_path))
    assert code == 1 and "needs a database connection" in out
    code, out = odcl(capsys, monkeypatch, url, tokens["ana"], "privacy", "propose", "--write", str(tmp_path / "review.jsonl"))
    assert code == 1 and "--write cannot go through a server" in out
    code, out = odcl(capsys, monkeypatch, url, tokens["ana"], "users", "tokens", "rex")
    assert code == 1 and "NAME cannot go through a server" in out
