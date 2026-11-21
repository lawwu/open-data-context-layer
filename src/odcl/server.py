"""`odcl serve`: the catalog over HTTP, and the web page that reads it.

Every read delegates to the `Catalog` operation of the same name, so the API answers exactly what the CLI and the
MCP tools answer. It runs on the standard library's threaded HTTP server, with one database connection per request.

    GET  /v1/overview
    GET  /v1/search?q=&kind=&layer=&limit=&retired=
    GET  /v1/entries/{key}                       one entry in full
    GET  /v1/entries/{key}/diff?since=&until=    what changed, each change labelled cosmetic, additive, semantic or breaking
    GET  /v1/entries/{key}/quality?at=           the newest result of each data-quality test, as of a time
    GET  /v1/glossary?domain=                    every glossary term
    GET  /v1/popular?since=30d&limit=            the tables most read by query jobs
    GET  /v1/privacy/proposals?table=&all=&min_confidence=   columns that look like personal data, for review
    POST /v1/terms, POST /v1/domains             editor: add or change a term {name, description, synonyms, domain, owner, status} or a domain
    POST /v1/entries/{key}/terms, /domain        editor: {terms: [...]} a table or column means; {domain} of a table, project or term
    POST /v1/quality                             importer: {source, results: [{entry, test, status, failures, observed_at, run, kind, message}]}
    GET  /v1/entries/{key}/lineage?direction=    up, down or both
    GET  /v1/entries/{key}/used-by
    GET  /v1/entries/{key}/documents             the documents that describe it
    GET  /v1/tables?layer=&system=&retired=       every table, with its columns and users counted (retired ones with retired=1)
    GET  /v1/privacy?pii_only=&table=             every column's personal-data flag, scrubbing, data category and retention
    GET  /v1/projects                            every project, with its uses and latest model version
    GET  /v1/projects/{name}/uses?upstream=
    GET  /v1/query-history?table=&since=7d&patterns=
    GET  /v1/datasets/{reference}/trace
    GET  /v1/models?project=
    GET  /v1/models/{name}/versions/{version}/trace
    GET  /v1/runs?system=&limit=
    POST /api/v1/lineage                         an OpenLineage event (the path OpenLineage's HTTP transport uses)

Who is asking, and what they may do (see `odcl.users`):

    GET  /v1/me                                  the signed-in user, or null
    GET  /v1/me/tokens, POST /v1/me/tokens       your API tokens; make one (shown once)
    POST /v1/me/tokens/{id}/revoke
    GET  /v1/users, POST /v1/users               admin: every user; make one (a person, or a service user for importers)
    POST /v1/users/{name}                        admin: change a user's role, or disable them
    GET  /v1/alerts/subscriptions                your alert subscriptions (an admin's: all of them)
    POST /v1/alerts/subscriptions                editor: {project | owner | all, channel, address, min_severity}
    POST /v1/alerts/subscriptions/{id}/remove    editor
    POST /v1/alerts/send                         importer: send what is new ({"dry_run": true} to only say)
    POST /v1/users/{name}/tokens                 admin: a token for a user, such as a service user
    POST /v1/projects                            editor: register a project
    POST /v1/projects/{name}/uses                editor: record that a project used tables or columns
    POST /v1/privacy                             editor: privacy fields of tables or columns
    POST /v1/entries/{key}/created-by            editor: what created a table
    POST /v1/entries/{key}/references            editor: a column's foreign keys
    POST /v1/entries/{key}/instances             editor: a definition table's copies
    POST /v1/models/{name}/versions/{version}/relied-on   editor: documents a model version relied on

A caller signs in with an API token (`Authorization: Bearer odcl_...`) or, with single sign-on set up (`odcl.sso`),
in the browser. Writes always need a signed-in user whose role allows them. Reads need one only with
`--require-sign-in` or single sign-on; otherwise anyone who can reach the server can read the catalog, which is why it
listens on 127.0.0.1 unless told otherwise. OpenLineage events are taken only with `--accept-lineage`, from an
importer's token or, when `ODCL_LINEAGE_TOKEN` is set, that token.
"""
from __future__ import annotations

import hmac
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import alerts, pii, popularity, quality
from . import sso as single_sign_on
from . import users
from .store import Catalog, CatalogError, require

MAX_BODY = 5 * 1024 * 1024
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}
POLICY = "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
STATUS = {"not_found": 404, "database": 503, "schema": 503, "unauthorized": 401, "forbidden": 403, "exists": 409}


def _span(value: str):
    from .cli import span
    return span(value)


def _explain_dataset(catalog: Catalog, reference: str) -> dict:
    from .builds import explain_dataset
    return explain_dataset(catalog, reference)


def _flag(value: str | None) -> bool:
    return (value or "").lower() in ("1", "true", "yes")


def _limit(value: str | None, default: int) -> int:
    require(value is None or value.isdigit(), "limit", "limit is a whole number")
    return int(value) if value else default


def _fields(body, allowed: tuple[str, ...], required: tuple[str, ...] = ()) -> dict:
    require(isinstance(body, dict), "body", "Send a JSON object")
    unknown = sorted(set(body) - set(allowed))
    require(not unknown, "body", f"Unknown field{'s' if len(unknown) > 1 else ''} {', '.join(unknown)}; send {', '.join(allowed)}")
    missing = [f for f in required if body.get(f) in (None, "", [])]
    require(not missing, "body", f"Send {', '.join(missing)}")
    return body


def _privacy(c: Catalog, b) -> list[dict]:
    b = _fields(b, ("keys", "pii", "scrubbing", "data_category", "retention"), ("keys",))
    return c.set_privacy(b["keys"], **{k: v for k, v in b.items() if k != "keys"})


def _subscribe(c: Catalog, b) -> dict:
    b = _fields(b, ("project", "owner", "all", "channel", "address", "min_severity"), ("channel", "address"))
    return alerts.subscribe(c, project=b.get("project"), owner=b.get("owner"), everything=bool(b.get("all")), channel=b["channel"],
                            address=b["address"], min_severity=b.get("min_severity") or "semantic")


PROJECT = ("name", "description", "owner", "repository", "status", "tags")

# (method, pattern, role, handler(catalog, match, query, body)). `anyone` needs no sign-in; `reader` needs one only
# when the server asks for sign-in; any other role always needs a signed-in user with that role or above.
ROUTES = [
    ("GET", r"/v1/overview", "reader", lambda c, m, q, b: c.overview()),
    ("GET", r"/v1/search", "reader", lambda c, m, q, b: c.find(q.get("q", ""), q.get("kind") or None, q.get("layer") or None, _limit(q.get("limit"), 50),
                                                                     retired=_flag(q.get("retired")))),
    ("GET", r"/v1/entries/(?P<key>[^/]+)", "reader", lambda c, m, q, b: c.get(m["key"])),
    ("GET", r"/v1/entries/(?P<key>[^/]+)/lineage", "reader", lambda c, m, q, b: c.lineage(m["key"], q.get("direction", "both"))),
    ("GET", r"/v1/entries/(?P<key>[^/]+)/used-by", "reader", lambda c, m, q, b: c.used_by(m["key"])),
    ("GET", r"/v1/entries/(?P<key>[^/]+)/documents", "reader", lambda c, m, q, b: c.documents(m["key"])),
    ("GET", r"/v1/entries/(?P<key>[^/]+)/quality", "reader", lambda c, m, q, b: quality.for_entry(c, m["key"], q.get("at") or None)),
    ("GET", r"/v1/entries/(?P<key>[^/]+)/diff", "reader", lambda c, m, q, b: c.diff(m["key"], q.get("since") or None, q.get("until") or None)),
    ("GET", r"/v1/projects/(?P<name>[^/]+)/uses", "reader", lambda c, m, q, b: c.uses(m["name"], _flag(q.get("upstream")))),
    ("GET", r"/v1/query-history", "reader", lambda c, m, q, b: c.table_history(q.get("table", ""), _span(q.get("since", "7d")), _flag(q.get("patterns")),
                                                                             _limit(q.get("limit"), 100))),
    ("GET", r"/v1/datasets/(?P<reference>[^/]+)/trace", "reader", lambda c, m, q, b: _explain_dataset(c, m["reference"])),
    ("GET", r"/v1/tables", "reader", lambda c, m, q, b: c.tables(q.get("layer") or None, q.get("system") or None, retired=_flag(q.get("retired")),
                                                                     sort=q.get("sort") or "key")),
    ("GET", r"/v1/projects", "reader", lambda c, m, q, b: c.projects()),
    ("GET", r"/v1/privacy", "reader", lambda c, m, q, b: c.privacy(_flag(q.get("pii_only")), q.get("table") or None)),
    ("GET", r"/v1/models", "reader", lambda c, m, q, b: c.models(q.get("project") or None)),
    ("GET", r"/v1/models/(?P<name>[^/]+)/versions/(?P<version>[^/]+)/trace", "reader", lambda c, m, q, b: c.explain_model(m["name"], m["version"])),
    ("GET", r"/v1/runs", "reader", lambda c, m, q, b: c.runs(q.get("system") or None, _limit(q.get("limit"), 100))),
    # who is asking
    ("GET", r"/v1/me", "anyone", lambda c, m, q, b: {"user": c.user}),
    ("GET", r"/v1/me/tokens", "reader", lambda c, m, q, b: users.tokens(c)),
    ("POST", r"/v1/me/tokens", "reader", lambda c, m, q, b: users.make_token(c, None, _fields(b, ("label",)).get("label"))),
    ("POST", r"/v1/me/tokens/(?P<id>[^/]+)/revoke", "reader", lambda c, m, q, b: users.revoke(c, m["id"])),
    ("GET", r"/v1/users", "admin", lambda c, m, q, b: users.listing(c)),
    ("POST", r"/v1/users", "admin", lambda c, m, q, b: users.add(c, **_fields(b, ("name", "role", "display_name", "email"), ("name", "role")))),
    ("POST", r"/v1/users/(?P<name>[^/]+)", "admin", lambda c, m, q, b: users.update(c, m["name"], **_fields(b, ("role", "disabled")))),
    ("POST", r"/v1/users/(?P<name>[^/]+)/tokens", "admin", lambda c, m, q, b: users.make_token(c, m["name"], _fields(b, ("label",)).get("label"))),
    ("GET", r"/v1/privacy/proposals", "reader", lambda c, m, q, b: pii.propose(c, q.get("table") or None, _flag(q.get("all")), q.get("min_confidence") or "low")),
    ("GET", r"/v1/popular", "reader", lambda c, m, q, b: popularity.popular(c, _span(q.get("since", "30d")), _limit(q.get("limit"), 50))),
    ("GET", r"/v1/glossary", "reader", lambda c, m, q, b: c.glossary(q.get("domain") or None)),
    ("POST", r"/v1/terms", "editor", lambda c, m, q, b: c.put("term", _fields(b, ("name", "description", "owner", "status", "synonyms", "domain"),
                                                                              ("name", "description")), merge=True)),
    ("POST", r"/v1/domains", "editor", lambda c, m, q, b: c.put("domain", _fields(b, ("name", "description", "owner"), ("name", "description")), merge=True)),
    ("POST", r"/v1/entries/(?P<key>[^/]+)/terms", "editor", lambda c, m, q, b: c.set_terms(m["key"], _fields(b, ("terms",)).get("terms") or [])),
    ("POST", r"/v1/entries/(?P<key>[^/]+)/domain", "editor", lambda c, m, q, b: c.set_domain(m["key"], _fields(b, ("domain",)).get("domain"))),
    ("POST", r"/v1/quality", "importer", lambda c, m, q, b: quality.record(c, _fields(b, ("source", "results"), ("source", "results"))["results"], b["source"])),
    # alerts
    ("GET", r"/v1/alerts/subscriptions", "reader", lambda c, m, q, b: alerts.subscriptions(c)),
    ("POST", r"/v1/alerts/subscriptions", "editor", lambda c, m, q, b: _subscribe(c, b)),
    ("POST", r"/v1/alerts/subscriptions/(?P<id>\d+)/remove", "editor", lambda c, m, q, b: alerts.unsubscribe(c, int(m["id"]))),
    ("POST", r"/v1/alerts/send", "importer", lambda c, m, q, b: alerts.send(c, bool(_fields(b or {}, ("dry_run",)).get("dry_run")))),
    # annotations
    ("POST", r"/v1/projects", "editor", lambda c, m, q, b: c.put("project", _fields(b, PROJECT, ("name", "description")))),
    ("POST", r"/v1/projects/(?P<name>[^/]+)/uses", "editor",
     lambda c, m, q, b: c.use(m["name"], _fields(b, ("keys", "purpose"), ("keys",))["keys"], b.get("purpose"))),
    ("POST", r"/v1/privacy", "editor", lambda c, m, q, b: {"changed": _privacy(c, b)}),
    ("POST", r"/v1/entries/(?P<key>[^/]+)/created-by", "editor", lambda c, m, q, b: c.set_created_by(m["key"], _fields(b, ("created_by",)).get("created_by"))),
    ("POST", r"/v1/entries/(?P<key>[^/]+)/references", "editor", lambda c, m, q, b: c.set_references(m["key"], _fields(b, ("columns",)).get("columns") or [])),
    ("POST", r"/v1/entries/(?P<key>[^/]+)/instances", "editor", lambda c, m, q, b: c.set_instances(m["key"], _fields(b, ("patterns",)).get("patterns") or [])),
    ("POST", r"/v1/models/(?P<name>[^/]+)/versions/(?P<version>[^/]+)/relied-on", "editor",
     lambda c, m, q, b: c.rely(m["name"], m["version"], _fields(b, ("documents", "why"), ("documents",))["documents"], b.get("why"))),
]


class Refused(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


class Handler(BaseHTTPRequestHandler):
    server_version = "odcl"
    dsn: str | None = None
    actor: str | None = None
    accept_lineage = False
    require_sign_in = False
    sso: single_sign_on.Settings | None = None

    def log_message(self, format, *args):  # noqa: A002 - the base class names it so
        if os.environ.get("ODCL_SERVE_LOG"):
            super().log_message(format, *args)

    # ---- responses -------------------------------------------------------------------------------------------
    def _send(self, status: int, body: bytes, kind: str, extra: dict | None = None) -> None:
        self.send_response(status)
        headers = {"Content-Type": kind, "Content-Length": str(len(body)), "X-Content-Type-Options": "nosniff",
                   "Referrer-Policy": "no-referrer", "Cache-Control": "no-store", **(extra or {})}
        for name, value in headers.items():
            for one in value if isinstance(value, list) else [value]:
                self.send_header(name, one)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, value) -> None:
        self._send(status, json.dumps(value, default=str).encode(), "application/json")

    def _error(self, status: int, code: str, message: str) -> None:
        error = {"code": code, "message": message}
        if status == 401 and self.sso:
            error["sign_in"] = "/auth/login"
        self._json(status, {"error": error})

    def _redirect(self, location: str, cookies: list[str] | None = None, status: int = 302) -> None:
        self._send(status, b"", "text/plain; charset=utf-8", {"Location": location, **({"Set-Cookie": cookies} if cookies else {})})

    # ---- who is asking ---------------------------------------------------------------------------------------
    def _origin(self) -> str:
        """This server as the browser reaches it: from ODCL_OIDC_REDIRECT_URI when that is set (behind a proxy that
        ends TLS), else the Host the request came to. Never X-Forwarded-Host: anyone can send it."""
        if self.sso and self.sso.redirect_uri:
            url = urlsplit(self.sso.redirect_uri)
            return f"{url.scheme}://{url.netloc}"
        return f"http://{self.headers.get('Host') or '%s:%s' % self.server.server_address[:2]}"

    def _bearer(self) -> str | None:
        auth = self.headers.get("Authorization")
        if auth is None:
            return None
        if not auth.startswith("Bearer ") or not auth[7:].strip():
            raise Refused(401, "unauthorized", "Send a token as Authorization: Bearer TOKEN")
        return auth[7:].strip()

    def _identify(self, catalog: Catalog, token: str | None) -> None:
        """Sign the catalog in as the caller: the token's user, else (with single sign-on) the browser's session. A
        session counts for a change only when the change comes from this server's own page (its Origin), so another
        site cannot make one in a signed-in browser's name."""
        if token is not None:
            catalog.user = users.authenticate(catalog.db, token)
        elif self.sso and (secret := single_sign_on.read_cookie(self.headers.get("Cookie"), single_sign_on.SESSION_COOKIE)):
            if self.command not in ("GET", "HEAD"):
                origin = self.headers.get("Origin")
                if origin is None or urlsplit(origin).netloc != (self.headers.get("Host") or ""):
                    raise Refused(403, "forbidden", "A change made with a browser session has to come from this server's own page")
            catalog.user = users.session_user(catalog.db, secret)
        if catalog.user:
            catalog.actor = catalog.user["name"]

    # ---- requests --------------------------------------------------------------------------------------------
    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_GET(self):  # noqa: N802
        url = urlsplit(self.path)
        if url.path == "/auth/login":
            return self._login(url)
        if url.path == "/auth/callback":
            return self._callback(url)
        if url.path in STATIC:
            if url.path == "/" and self.sso and not self._signed_in_browser():
                return self._redirect("/auth/login?next=" + quote(single_sign_on.safe_next(url.path + (f"?{url.query}" if url.query else ""))))
            name, kind = STATIC[url.path]
            body = resources.files("odcl").joinpath("web", name).read_bytes()
            return self._send(200, body, kind, {"Content-Security-Policy": POLICY, "X-Frame-Options": "DENY"})
        self._route("GET", url)

    def do_POST(self):  # noqa: N802
        url = urlsplit(self.path)
        if url.path in ("/api/v1/lineage", "/v1/lineage"):
            return self._lineage()
        if url.path == "/auth/logout":
            return self._logout()
        self._route("POST", url)

    def _route(self, method: str, url) -> None:
        path = url.path.rstrip("/") or "/"
        found = [(allowed, role, handler, match) for allowed, pattern, role, handler in ROUTES if (match := re.fullmatch(pattern, path))]
        if not found:
            return self._error(404, "not_found", f"Nothing at {path}")
        chosen = next((f for f in found if f[0] == method), None)
        if chosen is None:
            return self._error(405, "method", f"{path} answers {' and '.join(sorted({f[0] for f in found}))}")
        _, role, handler, match = chosen
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        params = {k: unquote(v) for k, v in match.groupdict().items()}
        try:
            body = self._body() if method == "POST" else None
        except Refused as e:
            return self._error(e.status, e.code, str(e))
        self._answer(lambda c: handler(c, params, query, body), role)

    def _body(self):
        length = self.headers.get("Content-Length", "")
        if not length.isdigit() or int(length) > MAX_BODY:
            raise Refused(413 if length.isdigit() else 411, "body", f"Send a body of at most {MAX_BODY} bytes with a Content-Length")
        raw = self.rfile.read(int(length))
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise Refused(400, "body", "The body is not JSON") from None

    def _answer(self, operation, role: str, created: bool = False, token: str | None = None, trusted: bool = False) -> None:
        """Open the catalog as the caller, check they may do this, and answer what `operation` returns. `trusted`
        (the shared lineage token) lets an unsigned request through as the server's own actor."""
        try:
            token = token if token is not None or trusted else self._bearer()
            with Catalog(self.dsn, actor=self.actor) as catalog:
                self._identify(catalog, token)
                needs_user = role != "anyone" and (role != "reader" or self.require_sign_in or self.sso is not None)
                if needs_user and catalog.user is None and not trusted:
                    raise Refused(401, "unauthorized", "Sign in" + (" in the browser, or send" if self.sso else ": send") + " an API token as Authorization: Bearer TOKEN")
                if role != "anyone":
                    catalog.allow(role)
                result = operation(catalog)
        except Refused as e:
            return self._error(e.status, e.code, str(e))
        except CatalogError as e:
            return self._error(STATUS.get(e.code, 400), e.code, str(e))
        status = 201 if created and isinstance(result, dict) and result.get("change") == "recorded" else 200
        self._json(status, result)

    def _lineage(self) -> None:
        if not self.accept_lineage:
            return self._error(403, "read_only", "This server does not accept OpenLineage events. Start it with odcl serve --accept-lineage")
        try:
            bearer = self._bearer()
        except Refused as e:
            return self._error(e.status, e.code, str(e))
        shared = os.environ.get("ODCL_LINEAGE_TOKEN")
        trusted = bool(shared and bearer and hmac.compare_digest(bearer.encode(), shared.encode()))
        if not trusted and bearer is None and (shared or self.require_sign_in or self.sso):
            return self._error(401, "unauthorized", "Send the lineage token, or an importer's API token, as Authorization: Bearer TOKEN")
        trusted = trusted or bearer is None  # no token is asked for: as before users, the server's own actor records it
        try:
            event = self._body()
        except Refused as e:
            return self._error(e.status, e.code, "Send one OpenLineage event: " + str(e))
        from . import openlineage
        self._answer(lambda c: openlineage.ingest(c, event), "importer", created=True, token=None if trusted else bearer, trusted=trusted)

    # ---- single sign-on ----------------------------------------------------------------------------------------
    def _signed_in_browser(self) -> bool:
        secret = single_sign_on.read_cookie(self.headers.get("Cookie"), single_sign_on.SESSION_COOKIE)
        if not secret:
            return False
        try:
            with Catalog(self.dsn, actor=self.actor) as catalog:
                return users.session_user(catalog.db, secret) is not None
        except CatalogError:
            return False

    def _login(self, url) -> None:
        if not self.sso:
            return self._error(404, "not_found", "Single sign-on is not set up on this server")
        try:
            location, cookie = single_sign_on.start_login(self.sso, self._origin(), parse_qs(url.query).get("next", ["/"])[-1])
        except CatalogError as e:
            return self._error(502, e.code, str(e))
        self._redirect(location, [cookie])

    def _problem(self, status: int, message: str) -> None:
        secure = self._origin().startswith("https:")
        self._send(status, (message + "\n").encode(), "text/plain; charset=utf-8", {"Set-Cookie": single_sign_on.clear_login_cookie(secure)})

    def _callback(self, url) -> None:
        if not self.sso:
            return self._error(404, "not_found", "Single sign-on is not set up on this server")
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        if query.get("error"):
            return self._problem(403, f"Signing in did not finish. The sign-in service said: {query.get('error_description') or query['error']}")
        origin = self._origin()
        try:
            who = single_sign_on.finish_login(self.sso, origin, query, self.headers.get("Cookie"))
        except CatalogError as e:
            if e.code == "sign_in":
                return self._problem(400, "This sign-in did not start in this browser. Open the page you wanted again, and sign in from there.")
            return self._problem(502, f"Could not finish signing in: {e}")
        try:
            with Catalog(self.dsn, actor=self.actor) as catalog:
                user = users.sign_in(catalog.db, who, single_sign_on.first_role(self.sso, who))
                secret = users.start_session(catalog.db, user["name"], self.sso.session_hours)
        except CatalogError as e:
            return self._problem(403 if e.code == "forbidden" else 503, f"You cannot sign in: {e}")
        secure = origin.startswith("https:")
        self._redirect(who["next"], [single_sign_on.cookie(single_sign_on.SESSION_COOKIE, secret, max_age=round(self.sso.session_hours * 3600), secure=secure),
                                     single_sign_on.clear_login_cookie(secure)])

    def _logout(self) -> None:
        """End this browser's session, and answer where to go next: the provider's own sign-out, where it offers one."""
        secret = single_sign_on.read_cookie(self.headers.get("Cookie"), single_sign_on.SESSION_COOKIE)
        if secret:
            try:
                with Catalog(self.dsn, actor=self.actor) as catalog:
                    users.end_session(catalog.db, secret)
            except CatalogError as e:
                return self._error(STATUS.get(e.code, 400), e.code, str(e))
        origin = self._origin()
        go = single_sign_on.logout_url(self.sso, origin) if self.sso else "/"
        body = json.dumps({"signed_out": True, "next": go}).encode()
        self._send(200, body, "application/json", {"Set-Cookie": single_sign_on.cookie(single_sign_on.SESSION_COOKIE, "", max_age=0, secure=origin.startswith("https:"))})


def make_server(dsn: str | None = None, *, host: str = "127.0.0.1", port: int = 8080, actor: str | None = None,
                accept_lineage: bool = False, require_sign_in: bool = False, sso: single_sign_on.Settings | None | bool = True) -> ThreadingHTTPServer:
    """A server ready to `serve_forever()`. The database is checked once now, so a wrong URL fails at start.

    Single sign-on is read from the environment (ODCL_OIDC_*) unless `sso` gives the settings, or False turns it off.
    With it on, every route needs a signed-in user."""
    settings = single_sign_on.settings() if sso is True else (sso or None)
    if settings:
        import jwt  # noqa: F401 - single sign-on needs the sso extra; say so at start, not at the first sign-in
    with Catalog(dsn, actor=actor):
        pass
    handler = type("OdclHandler", (Handler,), {"dsn": dsn, "actor": actor or "openlineage", "accept_lineage": accept_lineage,
                                               "require_sign_in": require_sign_in or settings is not None, "sso": settings})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
