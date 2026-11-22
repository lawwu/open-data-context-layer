"""What the HTTP server, single sign-on and outgoing requests refuse: one test for each problem a review found."""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_sso import odcl, provider, request, sign_in  # noqa: F401 - fixtures and helpers

from odcl import Catalog, CatalogError, alerts, users
from odcl.server import make_server

EVENT = {"eventType": "COMPLETE", "eventTime": "2026-01-01T00:00:00Z", "producer": "x",
         "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent", "run": {"runId": "01890000-0000-7000-8000-000000000000"},
         "job": {"namespace": "ns", "name": "job"}, "inputs": [{"namespace": "postgres://h:5432", "name": "db.s.a"}], "outputs": []}


def call(url, method="GET", body=None, token=None):
    headers = {**({"Authorization": f"Bearer {token}"} if token else {}), **({"Content-Type": "application/json"} if body is not None else {})}
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


@pytest.fixture
def people(database):
    with Catalog(database, actor="root") as c:
        for name, role in (("ana", "admin"), ("bob", "editor"), ("rita", "reader")):
            users.add(c, name, role)
        return {name: users.make_token(c, name) for name in ("ana", "bob", "rita")}


@pytest.fixture
def served(database):
    servers = []

    def start(**options):
        server = make_server(database, host="127.0.0.1", port=0, sso=False, **options)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"
    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def run_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---- single sign-on: only a verified email counts ------------------------------------------------------------------
def me(url, session):
    return json.loads(request(f"{url}/v1/me", cookies={"odcl_session": session})[2])["user"]


@pytest.mark.parametrize("verified", [None, "false", False])
def test_an_admin_email_the_provider_does_not_say_is_verified_is_not_an_admin(provider, odcl, verified):  # noqa: F811
    claims = {} if verified is None else {"email_verified": verified}
    _, session = sign_in(provider, odcl, sub=f"s-{verified}", email="ana@example.com", **claims)
    assert me(odcl, session)["role"] == "reader"


@pytest.mark.parametrize("claims", [{"email_verified": True}, {"email_verified": "true"}, {"xms_edov": True}])
def test_a_verified_admin_email_is_an_admin(provider, odcl, claims):  # noqa: F811
    _, session = sign_in(provider, odcl, sub="s-ana", email="ana@example.com", **claims)
    assert me(odcl, session)["role"] == "admin"


def test_a_user_an_admin_made_is_not_taken_over_by_an_unverified_email(provider, odcl, database):  # noqa: F811
    with Catalog(database) as direct:
        users.add(direct, "boss", "admin", email="boss@example.com")
    _, session = sign_in(provider, odcl, sub="someone-else", email="boss@example.com")
    assert (me(odcl, session)["name"], me(odcl, session)["role"]) != ("boss", "admin")


def test_a_sign_in_cookie_that_was_changed_is_refused(provider, odcl):  # noqa: F811
    status, headers, _, started = request(f"{odcl}/auth/login?next=/x")
    started["odcl_oidc"] = started["odcl_oidc"][:-50] + "A" * 50  # what it holds, or its signature, changed
    status, _, body, _ = request(provider.approve(headers["Location"], sub="s-x", email="x@example.com"), cookies=started)
    assert status == 400 and b"did not start in this browser" in body


def test_a_browser_change_through_a_proxy_is_checked_against_the_public_address(provider, odcl, monkeypatch):  # noqa: F811
    _, session = sign_in(provider, odcl, sub="ed", email="ed@example.com", email_verified=True, roles=["Odcl.Editor"])
    post = lambda origin: request(f"{odcl}/v1/projects", "POST", cookies={"odcl_session": session},  # noqa: E731
                                  body={"name": "p", "description": "d"}, headers={"Origin": origin})[0]
    monkeypatch.setenv("ODCL_PUBLIC_URL", "https://catalog.example.com")  # nginx sends the upstream's Host, not this
    assert post("https://catalog.example.com") == 200
    assert post("https://evil.example.com") == 403 and post("http://catalog.example.com") == 403


def test_single_sign_on_on_an_address_others_reach_needs_https(provider, database, monkeypatch):  # noqa: F811
    for name, value in {"ODCL_OIDC_ISSUER": provider.issuer, "ODCL_OIDC_CLIENT_ID": "odcl", "ODCL_OIDC_CLIENT_SECRET": "s",
                        "ODCL_OIDC_ALLOW_INSECURE_HTTP": "true"}.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(CatalogError, match="https://"):
        make_server(database, host="0.0.0.0", port=0)


# ---- someone who did not sign in is not a trusted connection ----------------------------------------------------
def test_no_one_revokes_a_token_without_signing_in_and_a_reader_learns_nothing_of_others(served, people):
    url = served()
    assert call(f"{url}/v1/me/tokens/{people['ana']['id']}/revoke", "POST", {})[0] == 401
    assert call(f"{url}/v1/me", token=people["ana"]["token"])[1]["user"]["name"] == "ana"  # still works
    unknown = call(f"{url}/v1/me/tokens/tok_000000000000/revoke", "POST", {}, token=people["rita"]["token"])
    someone_elses = call(f"{url}/v1/me/tokens/{people['bob']['id']}/revoke", "POST", {}, token=people["rita"]["token"])
    assert unknown[0] == someone_elses[0] == 404
    assert call(f"{url}/v1/me/tokens/{people['rita']['id']}/revoke", "POST", {}, token=people["rita"]["token"])[0] == 200  # her own


def test_every_write_needs_a_signed_in_user(served, people):
    url = served()
    status, body = call(f"{url}/v1/alerts/send", "POST", {"dry_run": True})
    assert status == 401


def test_no_one_reads_subscriptions_without_signing_in_and_a_webhook_s_secret_is_not_shown(served, people, database):
    with Catalog(database, token=people["bob"]["token"]) as bob:
        alerts.subscribe(bob, everything=True, channel="email", address="bob.private@example.com")
        alerts.subscribe(bob, everything=True, channel="webhook", address="https://user:s3cret@hooks.example.com:8443/abc")
    url = served()
    assert call(f"{url}/v1/alerts/subscriptions") == (200, [])
    status, listed = call(f"{url}/v1/alerts/subscriptions", token=people["bob"]["token"])
    assert [s["address"] for s in listed] == ["bob.private@example.com", "https://hooks.example.com:8443/…"]


def test_once_there_are_users_an_openlineage_event_needs_a_token(served, people, monkeypatch):
    url = served(accept_lineage=True)
    assert call(f"{url}/api/v1/lineage", "POST", EVENT)[0] == 401
    assert call(f"{url}/api/v1/lineage", "POST", EVENT, token=people["rita"]["token"])[0] == 403  # a reader is not an importer
    monkeypatch.setenv("ODCL_LINEAGE_TOKEN", "shared-secret")
    assert call(f"{url}/api/v1/lineage", "POST", EVENT, token="shared-secret")[0] == 201


def test_before_anyone_signs_up_an_openlineage_event_is_accepted_as_before(served):
    assert call(f"{served(accept_lineage=True)}/api/v1/lineage", "POST", EVENT)[0] == 201


def test_a_request_that_breaks_something_gets_an_answer(served):
    status, body = call(f"{served()}/v1/entries/%00/diff")
    assert status in (400, 500) and "error" in body


# ---- outgoing requests go only where they were sent ------------------------------------------------------------
class Redirecting(BaseHTTPRequestHandler):
    target = ""

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        self.send_response(302)
        self.send_header("Location", self.target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST  # noqa: N815


def test_a_webhook_that_redirects_is_a_failed_delivery_and_is_not_followed(monkeypatch):
    hits = []

    class Inside(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
    inside = run_server(Inside)
    hook = run_server(type("Hook", (Redirecting,), {"target": f"http://127.0.0.1:{inside.server_address[1]}/metadata"}))
    monkeypatch.setenv("ODCL_ALERT_ALLOW_HOSTS", "127.0.0.1")
    with pytest.raises(CatalogError, match="answered 302"):
        alerts._post(f"http://127.0.0.1:{hook.server_address[1]}/hook", {"x": 1})
    assert hits == []
    for server in (inside, hook):
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("url", ["https://127.0.0.1/hook", "https://169.254.169.254/latest", "https://10.0.0.5/x", "http://localhost:9/x"])
def test_a_webhook_on_an_internal_address_is_refused_unless_an_admin_allows_it(url, monkeypatch):
    monkeypatch.delenv("ODCL_ALERT_ALLOW_HOSTS", raising=False)
    with pytest.raises(CatalogError, match="internal address|only over https"):
        alerts._post(url, {"x": 1})


def test_a_server_that_redirects_does_not_get_the_token_sent_on(monkeypatch):
    from odcl.mcp import Remote
    seen = []

    class Elsewhere(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
    elsewhere = run_server(Elsewhere)
    first = run_server(type("First", (Redirecting,), {"target": f"http://127.0.0.1:{elsewhere.server_address[1]}/v1/me"}))
    with pytest.raises(CatalogError, match="redirected"):
        Remote(f"http://localhost:{first.server_address[1]}", "odcl_SECRET")
    assert seen == []
    for server in (elsewhere, first):
        server.shutdown()
        server.server_close()
