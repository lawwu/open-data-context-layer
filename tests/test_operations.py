"""The operation table, and the three adapters over it: the command line, the HTTP API and the MCP tools."""
import inspect
import json
import re
import threading
import urllib.parse
import urllib.request

import pytest

from odcl import Catalog
from odcl.cli import main, parser
from odcl.mcp import TOOLS, Tools
from odcl.operations import OPERATIONS, prepare
from odcl.server import ROUTES, make_server


@pytest.mark.parametrize("op", OPERATIONS, ids=lambda op: op.name)
def test_every_operation_runs_with_exactly_its_params(op):
    """Each param is an argument `run` takes, and `run` needs nothing the table does not give it."""
    signature = inspect.signature(op.run)
    if any(p.kind == p.VAR_KEYWORD for p in signature.parameters.values()):
        return
    takes = [n for n, p in list(signature.parameters.items())[1:]]
    needs = [n for n, p in list(signature.parameters.items())[1:] if p.default is p.empty]
    assert {p.name for p in op.params} <= set(takes), f"{op.name}: params run does not take"
    assert set(needs) <= {p.name for p in op.params}, f"{op.name}: run needs what the table does not give"


def test_each_adapter_offers_what_the_table_says():
    commands = {op.cli for op in OPERATIONS if op.cli}
    subparsers = parser()._subparsers._group_actions[0].choices
    found = set()
    for name, sub in subparsers.items():
        nested = [a for a in sub._actions if a.__class__.__name__ == "_SubParsersAction"]
        found |= {f"{name} {inner}" for inner in nested[0].choices} if nested else {name}
    assert commands == found - {"migrate", "serve", "mcp"}
    assert sorted((m, re.sub(r"\(\?P<(\w+)>[^)]*\)", r"{\1}", p)) for m, p, _, _ in ROUTES) == sorted(
        tuple(op.http.split(" ", 1)) for op in OPERATIONS if op.http)
    assert [t["name"] for t in TOOLS] == [op.mcp for op in OPERATIONS if op.mcp]
    assert all(t["write"] == (t["op"].role != "reader") for t in TOOLS)


def test_prepare_fills_defaults_joins_words_and_reads_strings():
    find = next(op for op in OPERATIONS if op.name == "find")
    assert prepare(find, {"query": ["plan", "tier"], "limit": "10", "retired": "true"}) == {
        "query": "plan tier", "kind": None, "layer": None, "limit": 10, "retired": True}
    assert prepare(find, {})["limit"] == 50
    with pytest.raises(Exception, match="whole number"):
        prepare(find, {"limit": "many"})


@pytest.fixture
def served(database):
    with Catalog(database, actor="ana") as c:
        c.put("table", {"path": "core.sales.daily_totals", "description": "Totals by day"})
        c.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "NUMERIC"})
        c.put("column", {"table": "core.sales.daily_totals", "name": "net", "derived_from": ["core.sales.daily_totals.amount"]})
        c.put("project", {"name": "forecast", "description": "Daily forecast"})
        c.use("forecast", ["core.sales.daily_totals.net"])
        c.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "FLOAT64"})
    server = make_server(database, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield database, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("tool, args, cli, url", [
    ("get_entry", {"key": "core.sales.daily_totals.net"}, ["show", "core.sales.daily_totals.net"], "/v1/entries/core.sales.daily_totals.net"),
    ("trace_lineage", {"key": "core.sales.daily_totals.net", "direction": "up"}, ["lineage", "core.sales.daily_totals.net", "--direction", "up"],
     "/v1/entries/core.sales.daily_totals.net/lineage?direction=up"),
    ("used_by", {"key": "core.sales.daily_totals.amount"}, ["used-by", "core.sales.daily_totals.amount"], "/v1/entries/core.sales.daily_totals.amount/used-by"),
    ("what_changed", {"key": "core.sales.daily_totals.amount"}, ["diff", "core.sales.daily_totals.amount"], "/v1/entries/core.sales.daily_totals.amount/diff"),
    ("project_uses", {"project": "forecast", "upstream": True}, ["uses", "forecast", "--upstream"], "/v1/projects/forecast/uses?upstream=true"),
    ("find_data", {"query": "totals day"}, ["find", "totals", "day"], "/v1/search?q=totals+day"),
])
def test_the_command_line_the_http_api_and_mcp_answer_alike(served, capsys, tool, args, cli, url):
    database, base = served
    assert main(["--database-url", database, "--json", *cli]) == 0
    from_cli = json.loads(capsys.readouterr().out)
    with urllib.request.urlopen(base + url) as response:
        from_http = json.loads(response.read())
    with Catalog(database, actor="ana") as c:
        from_mcp = Tools(c).call(tool, args)
    if tool == "find_data":
        from_mcp = from_mcp["results"]
    assert from_cli == from_http == json.loads(json.dumps(from_mcp, default=str))


def test_a_write_says_what_it_takes_over_http(served):
    database, base = served
    from odcl import users
    with Catalog(database, actor="ana") as c:
        users.add(c, "ana", "editor")
        token = users.make_token(c, "ana")["token"]

    def post(path, body):
        request = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    assert post("/v1/entries/core.sales.daily_totals.net/references", {"columns": ["core.sales.daily_totals.amount"]})[0] == 200
    status, body = post("/v1/entries/core.sales.daily_totals.net/references", {"colour": "red"})
    assert status == 400 and body["error"]["message"] == "Unknown field colour; send columns"
    status, body = post("/v1/projects/" + urllib.parse.quote("forecast") + "/uses", {"purpose": "x"})
    assert status == 400 and body["error"]["message"] == "Send keys"


def test_the_api_describes_itself_as_the_table_does(served):
    """GET /v1/operations is what the web page's command palette builds its commands and forms from."""
    _, base = served
    with urllib.request.urlopen(base + "/v1/operations") as response:
        described = {d["name"]: d for d in json.loads(response.read())}
    assert set(described) == {op.name for op in OPERATIONS if op.http}
    for op in OPERATIONS:
        if not op.http:
            continue
        d = described[op.name]
        assert f"{d['method']} {d['path']}" == op.http and d["role"] == op.role
        in_path = set(re.findall(r"\{(\w+)\}", d["path"]))
        assert {p["name"] for p in d["params"] if p["in"] == "path"} == in_path
        assert all(p["required"] for p in d["params"] if p["in"] == "path")
        # every other field is one the route takes under that name
        assert {p["name"] for p in d["params"] if p["in"] != "path"} == {p.field for p in op.params if p.http is not None and p.name not in in_path}
    use = described["use"]
    assert [(p["name"], p["in"], p["kind"], p["required"]) for p in use["params"]] == [
        ("name", "path", "str", True), ("keys", "body", "list", True), ("purpose", "body", "str", False)]
    assert described["users_add"]["params"][1]["choices"] == ["reader", "editor", "importer", "admin"]
