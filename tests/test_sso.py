"""Single sign-on for `odcl serve`, against a fake OpenID Connect provider that checks PKCE and signs its ID tokens."""
import base64
import hashlib
import http.cookies
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

jwt = pytest.importorskip("jwt")
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from odcl import Catalog, sso, users  # noqa: E402
from odcl.server import make_server  # noqa: E402
from odcl.store import CatalogError  # noqa: E402

CLIENT, SECRET = "odcl-test", "client-secret"


class Provider:
    """Discovery, signing keys and a token endpoint. `approve` stands in for a person signing in at the provider: it
    takes the address odcl sent the browser to and answers the code the provider would send back."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.codes = {}
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, value):
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if self.path == "/.well-known/openid-configuration":
                    return self._json(200, provider.metadata())
                if self.path == "/jwks":
                    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(provider.key.public_key()))
                    return self._json(200, {"keys": [{**public, "kid": "k1", "use": "sig", "alg": "RS256"}]})
                self._json(404, {})

            def do_POST(self):  # noqa: N802
                form = {k: v[-1] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
                expected = "Basic " + base64.b64encode(f"{CLIENT}:{SECRET}".encode()).decode()
                if self.headers.get("Authorization") != expected:
                    return self._json(401, {"error": "invalid_client"})
                grant = provider.codes.pop(form.get("code"), None)
                challenge = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
                if not grant or grant["challenge"] != challenge or grant["redirect_uri"] != form.get("redirect_uri"):
                    return self._json(400, {"error": "invalid_grant"})
                now = int(time.time())
                claims = {"iss": provider.issuer, "aud": CLIENT, "iat": now, "exp": now + 300, "nonce": grant["nonce"], **grant["claims"]}
                self._json(200, {"access_token": "x", "token_type": "Bearer", "id_token": jwt.encode(claims, provider.key, "RS256", headers={"kid": "k1"})})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.issuer = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def metadata(self):
        return {"issuer": self.issuer, "authorization_endpoint": f"{self.issuer}/authorize", "token_endpoint": f"{self.issuer}/token",
                "jwks_uri": f"{self.issuer}/jwks", "end_session_endpoint": f"{self.issuer}/logout", "id_token_signing_alg_values_supported": ["RS256"]}

    def approve(self, location: str, **claims) -> str:
        asked = {k: v[-1] for k, v in parse_qs(urlsplit(location).query).items()}
        assert asked["client_id"] == CLIENT and asked["code_challenge_method"] == "S256" and asked["response_type"] == "code"
        code = secrets.token_hex(8)
        self.codes[code] = {"challenge": asked["code_challenge"], "nonce": asked["nonce"], "redirect_uri": asked["redirect_uri"], "claims": claims}
        return f"{asked['redirect_uri']}?{urlencode({'code': code, 'state': asked['state']})}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPEN = urllib.request.build_opener(NoRedirect)


def request(url, method="GET", cookies=None, body=None, headers=None):
    """status, headers, body, and the cookies set, without following redirects."""
    hdrs = {**({"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())} if cookies else {}), **(headers or {})}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, headers=hdrs, data=json.dumps(body).encode() if body is not None else None)
    try:
        response = OPEN.open(req)
    except urllib.error.HTTPError as e:
        response = e
    jar = http.cookies.SimpleCookie()
    for value in response.headers.get_all("Set-Cookie") or []:
        jar.load(value)
    return response.status, response.headers, response.read(), {k: m.value for k, m in jar.items()}


@pytest.fixture
def provider():
    p = Provider()
    sso.forget_provider()
    yield p
    p.server.shutdown()
    p.server.server_close()


@pytest.fixture
def odcl(database, provider, monkeypatch):
    """An odcl serve with single sign-on through the fake provider. People in Odcl.Editor become editors on their first sign-in."""
    env = {"ODCL_OIDC_ISSUER": provider.issuer, "ODCL_OIDC_CLIENT_ID": CLIENT, "ODCL_OIDC_CLIENT_SECRET": SECRET,
           "ODCL_OIDC_ALLOW_INSECURE_HTTP": "true", "ODCL_OIDC_ADMIN_EMAILS": "ana@example.com",
           "ODCL_OIDC_ROLE_CLAIM": "roles", "ODCL_OIDC_ROLE_VALUES": "Odcl.Editor=editor, Odcl.Importer=importer"}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    server = make_server(database, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def sign_in(provider, url, next_path="/", **claims):
    status, headers, _, started = request(f"{url}/auth/login?next={next_path}")
    assert status == 302 and "odcl_oidc" in started
    status, headers, _, cookies = request(provider.approve(headers["Location"], **claims), cookies=started)
    assert status == 302, _
    return headers["Location"], cookies["odcl_session"]


def test_signing_in_makes_the_user_and_a_session(provider, odcl, database):
    status, headers, _, _ = request(f"{odcl}/")
    assert status == 302 and headers["Location"] == "/auth/login?next=/"
    status, _, body, _ = request(f"{odcl}/v1/search?q=x")
    assert status == 401 and json.loads(body)["error"]["sign_in"] == "/auth/login"

    went, session = sign_in(provider, odcl, "/%23/tables", sub="s-ana", email="Ana@example.com", email_verified=True, name="Ana Lee",
                            preferred_username="ana.lee@example.com")
    assert went == "/#/tables"
    status, _, body, _ = request(f"{odcl}/v1/me", cookies={"odcl_session": session})
    me = json.loads(body)["user"]
    assert (me["name"], me["role"], me["email"], me["display_name"], me["single_sign_on"]) == ("ana.lee", "admin", "ana@example.com", "Ana Lee", True)
    assert request(f"{odcl}/", cookies={"odcl_session": session})[0] == 200

    _, ben = sign_in(provider, odcl, sub="s-ben", email="ben@example.com", roles=["Odcl.Editor"])
    _, cam = sign_in(provider, odcl, sub="s-cam", email="ana@example.com", email_verified=False, preferred_username="ana.lee")
    roles = {u["name"]: u["role"] for u in json.loads(request(f"{odcl}/v1/users", cookies={"odcl_session": session})[2])}
    assert roles == {"ana.lee": "admin", "ben": "editor", "ana.lee-2": "reader"}  # an unverified email never makes an admin

    # A later sign-in finds the same user by the provider's subject, and never changes the role.
    _, again = sign_in(provider, odcl, sub="s-ben", email="ben@example.com", roles=[])
    assert json.loads(request(f"{odcl}/v1/me", cookies={"odcl_session": again})[2])["user"]["role"] == "editor"


def test_a_session_changes_things_only_from_this_servers_own_page(provider, odcl):
    _, session = sign_in(provider, odcl, sub="s-ben", email="ben@example.com", roles=["Odcl.Editor"])
    project = {"name": "weekly_report", "description": "A weekly sales report"}
    host = urlsplit(odcl).netloc
    assert request(f"{odcl}/v1/projects", "POST", {"odcl_session": session}, project)[0] == 403
    assert request(f"{odcl}/v1/projects", "POST", {"odcl_session": session}, project, {"Origin": "https://elsewhere.example"})[0] == 403
    status, _, body, _ = request(f"{odcl}/v1/projects", "POST", {"odcl_session": session}, project, {"Origin": f"http://{host}"})
    assert status == 200 and json.loads(body)["registered_by"] == "ben"

    status, _, body, cleared = request(f"{odcl}/auth/logout", "POST", {"odcl_session": session})
    assert status == 200 and json.loads(body)["next"].startswith(f"{provider.issuer}/logout?") and cleared["odcl_session"] == ""
    assert request(f"{odcl}/v1/me", cookies={"odcl_session": session})[2] == b'{"user": null}'


def test_a_sign_in_must_start_in_the_same_browser(provider, odcl):
    status, headers, _, started = request(f"{odcl}/auth/login")
    back = provider.approve(headers["Location"], sub="s-x")
    assert request(back)[0] == 400  # no sign-in cookie
    forged = back.replace("state=", "state=x")
    assert request(forged, cookies=started)[0] == 400
    assert request(f"{odcl}/auth/callback?error=access_denied&error_description=Not+assigned", cookies=started)[0] == 403
    assert sso.safe_next("//evil.example/") == "/" and sso.safe_next("https://evil.example") == "/" and sso.safe_next("/#/runs") == "/#/runs"


def test_tokens_of_people_who_stop_signing_in_stop_working(provider, odcl, database):
    _, session = sign_in(provider, odcl, sub="s-ben", email="ben@example.com", roles=["Odcl.Editor"])
    status, _, body, _ = request(f"{odcl}/v1/me/tokens", "POST", {"odcl_session": session}, {"label": "laptop"}, {"Origin": f"http://{urlsplit(odcl).netloc}"})
    token = json.loads(body)["token"]
    assert request(f"{odcl}/v1/me", headers={"Authorization": f"Bearer {token}"})[0] == 200
    with Catalog(database) as direct:
        direct.db.execute("UPDATE users SET last_sign_in_at = now() - interval '31 days' WHERE name = 'ben'")
    status, _, body, _ = request(f"{odcl}/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert status == 401 and "has not signed in for 30" in json.loads(body)["error"]["message"]


def test_a_user_an_admin_made_is_taken_over_by_their_verified_email(provider, odcl, database):
    with Catalog(database) as direct:
        users.add(direct, "bea", "editor", email="bea@example.com")
    _, session = sign_in(provider, odcl, sub="s-bea", email="bea@example.com", email_verified=True)
    me = json.loads(request(f"{odcl}/v1/me", cookies={"odcl_session": session})[2])["user"]
    assert (me["name"], me["role"], me["single_sign_on"]) == ("bea", "editor", True)
    with Catalog(database) as direct:
        users.update(direct, "bea", disabled=True)
    assert json.loads(request(f"{odcl}/v1/me", cookies={"odcl_session": session})[2])["user"] is None
    status, headers, _, started = request(f"{odcl}/auth/login")
    assert request(provider.approve(headers["Location"], sub="s-bea", email="bea@example.com"), cookies=started)[0] == 403


def test_settings_are_all_or_nothing():
    assert sso.settings({}) is None
    with pytest.raises(CatalogError, match="ODCL_OIDC_CLIENT_SECRET"):
        sso.settings({"ODCL_OIDC_ISSUER": "https://id.example", "ODCL_OIDC_CLIENT_ID": "x"})
    with pytest.raises(CatalogError, match="ROLE_VALUES"):
        sso.settings({"ODCL_OIDC_ISSUER": "https://id.example", "ODCL_OIDC_CLIENT_ID": "x", "ODCL_OIDC_CLIENT_SECRET": "y", "ODCL_OIDC_ROLE_VALUES": "Odcl.X=owner"})
    s = sso.settings({"ODCL_OIDC_ISSUER": "https://id.example/", "ODCL_OIDC_CLIENT_ID": "x", "ODCL_OIDC_CLIENT_SECRET": "y"})
    assert (s.issuer, s.scopes, s.session_hours, s.token_idle_days, s.default_role) == ("https://id.example", "openid profile email", 12, 30, "reader")
