"""`odcl serve`: the HTTP API and the web page. Every read answers with what the Python operation answers."""
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pytest

from odcl import Catalog, vertex
from odcl.dbt import dbt_entries
from odcl.server import make_server
from test_dataform import SNAPSHOT, sync_history
from test_openlineage import EVENT
from test_vertex import EXPECTED, FakeVertex, build, load

DBT = Path(__file__).parent / "fixtures" / "dbt_medallion" / "target"
GOLD = "churn.main_gold.account_features"


@pytest.fixture
def serve(database):
    servers = []

    def start(**options):
        server = make_server(database, host="127.0.0.1", port=0, **options)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"
    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def catalog(database):
    with Catalog(database, actor="ana") as c:
        c.apply(dbt_entries(DBT))
        yield c


def call(url, method="GET", body=None, headers=None):
    request = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def get(url):
    status, _, body = call(url)
    assert status == 200, body
    return json.loads(body)


def test_reads_answer_what_the_catalog_answers(catalog, serve):
    api = serve() + "/v1"
    assert get(f"{api}/search?q=plan&kind=column") == catalog.find("plan", kind="column")
    assert get(f"{api}/entries/{quote('table:' + GOLD, safe='')}") == catalog.get(GOLD)
    assert get(f"{api}/entries/{quote(GOLD + '.tenure_months', safe='')}/lineage?direction=up") == catalog.lineage(GOLD + ".tenure_months", "up")
    assert get(f"{api}/entries/{quote('churn.bronze.accounts', safe='')}/used-by") == catalog.used_by("churn.bronze.accounts")
    assert get(f"{api}/overview")["counts"] == catalog.overview()["counts"]


def test_the_model_trace_is_the_same_over_http(database, serve):
    with Catalog(database, actor="ana") as catalog:
        sync_history(catalog)
        build(catalog)
        vertex.import_build(catalog, load("training_manifest.json"), client=FakeVertex())
    api = serve() + "/v1"
    assert get(f"{api}/models/staffing-forecast/versions/v17/trace") == EXPECTED
    assert get(f"{api}/models") == [{"model": "staffing-forecast", "version": "v17", "dataset_version": EXPECTED["read"]["dataset_version"],
                                     "artifact": EXPECTED["artifact"]["uri"], "recorded_by": "ana", "recorded_at": get(f"{api}/models")[0]["recorded_at"]}]
    trace = get(f"{api}/datasets/{quote(SNAPSHOT, safe='')}/trace")
    assert trace["produced_by"]["dataform_invocation"] == EXPECTED["inputs"][0]["produced_by"]["dataform_invocation"]
    assert [r["system"] for r in get(f"{api}/runs")] == ["vertex", "vertex", "dataform", "dataform"]


def test_what_is_not_there_or_not_valid_says_why(catalog, serve):
    api = serve() + "/v1"
    status, _, body = call(f"{api}/entries/nothing.here")
    assert status == 404 and json.loads(body)["error"]["code"] == "not_found"
    status, _, body = call(f"{api}/entries/{GOLD}/lineage?direction=sideways")
    assert status == 400 and json.loads(body)["error"]["code"] == "direction"
    assert call(f"{api}/no-such-thing")[0] == 404
    assert call(f"{api}/search", method="POST", body={})[0] == 405


def test_openlineage_events_are_accepted_only_when_the_server_was_started_to_accept_them(database, serve, monkeypatch):
    closed = serve()
    status, _, body = call(f"{closed}/api/v1/lineage", "POST", EVENT)
    assert status == 403 and "--accept-lineage" in json.loads(body)["error"]["message"]

    monkeypatch.setenv("ODCL_LINEAGE_TOKEN", "s3cret-for-tests")
    guarded = serve(accept_lineage=True)
    assert call(f"{guarded}/api/v1/lineage", "POST", EVENT)[0] == 401
    assert call(f"{guarded}/api/v1/lineage", "POST", EVENT, {"Authorization": "Bearer wrong"})[0] == 401
    status, _, body = call(f"{guarded}/api/v1/lineage", "POST", EVENT, {"Authorization": "Bearer s3cret-for-tests"})
    assert status == 201 and json.loads(body)["change"] == "recorded"
    assert call(f"{guarded}/api/v1/lineage", "POST", EVENT, {"Authorization": "Bearer s3cret-for-tests"})[0] == 200  # a replay
    [run] = get(f"{guarded}/v1/runs?system=openlineage")
    assert run["job"] == "airflow-prod/staffing_daily.build_weekly_hours"

    status, _, body = call(f"{guarded}/api/v1/lineage", "POST", {"eventType": "DONE"}, {"Authorization": "Bearer s3cret-for-tests"})
    assert status == 400 and json.loads(body)["error"]["code"] == "openlineage"


def test_the_web_page_is_served_with_a_policy_that_runs_only_its_own_script(serve):
    root = serve()
    status, headers, body = call(root + "/")
    assert status == 200 and headers["Content-Type"].startswith("text/html") and b"<script src=\"/app.js\"" in body
    assert "default-src 'self'" in headers["Content-Security-Policy"] and headers["X-Content-Type-Options"] == "nosniff"
    status, headers, _ = call(root + "/app.js")
    assert status == 200 and headers["Content-Type"].startswith("text/javascript")
    assert call(root + "/../pyproject.toml")[0] == 404 and call(root + "/%2e%2e/store.py")[0] == 404
