"""`odcl serve`: the catalog over HTTP, and the web page that reads it.

Every read delegates to the `Catalog` operation of the same name, so the API answers exactly what the CLI and the
MCP tools answer. It runs on the standard library's threaded HTTP server, with one database connection per request.

    GET  /v1/overview
    GET  /v1/search?q=&kind=&layer=&limit=
    GET  /v1/entries/{key}                       one entry in full
    GET  /v1/entries/{key}/lineage?direction=    up, down or both
    GET  /v1/entries/{key}/used-by
    GET  /v1/tables?layer=&system=                every table, with its columns and users counted
    GET  /v1/projects                            every project, with its uses and latest model version
    GET  /v1/projects/{name}/uses?upstream=
    GET  /v1/query-history?table=&since=7d&patterns=
    GET  /v1/datasets/{reference}/trace
    GET  /v1/models?project=
    GET  /v1/models/{name}/versions/{version}/trace
    GET  /v1/runs?system=&limit=
    POST /api/v1/lineage                         an OpenLineage event (the path OpenLineage's HTTP transport uses)

There is no sign-in: anyone who can reach the server can read the catalog, so it listens on 127.0.0.1 unless told
otherwise. Writes are off unless the server is started with `--accept-lineage`; set `ODCL_LINEAGE_TOKEN` to make
senders present it as a bearer token.
"""
from __future__ import annotations

import hmac
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, unquote, urlsplit

from .store import Catalog, CatalogError, require

MAX_BODY = 5 * 1024 * 1024
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}
POLICY = "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
NOT_FOUND = ("not_found",)


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


ROUTES = [  # (method, pattern, handler(catalog, match, query, body))
    ("GET", r"/v1/overview", lambda c, m, q, b: c.overview()),
    ("GET", r"/v1/search", lambda c, m, q, b: c.find(q.get("q", ""), q.get("kind") or None, q.get("layer") or None, _limit(q.get("limit"), 50))),
    ("GET", r"/v1/entries/(?P<key>[^/]+)", lambda c, m, q, b: c.get(m["key"])),
    ("GET", r"/v1/entries/(?P<key>[^/]+)/lineage", lambda c, m, q, b: c.lineage(m["key"], q.get("direction", "both"))),
    ("GET", r"/v1/entries/(?P<key>[^/]+)/used-by", lambda c, m, q, b: c.used_by(m["key"])),
    ("GET", r"/v1/projects/(?P<name>[^/]+)/uses", lambda c, m, q, b: c.uses(m["name"], _flag(q.get("upstream")))),
    ("GET", r"/v1/query-history", lambda c, m, q, b: c.table_history(q.get("table", ""), _span(q.get("since", "7d")), _flag(q.get("patterns")),
                                                                   _limit(q.get("limit"), 100))),
    ("GET", r"/v1/datasets/(?P<reference>[^/]+)/trace", lambda c, m, q, b: _explain_dataset(c, m["reference"])),
    ("GET", r"/v1/tables", lambda c, m, q, b: c.tables(q.get("layer") or None, q.get("system") or None)),
    ("GET", r"/v1/projects", lambda c, m, q, b: c.projects()),
    ("GET", r"/v1/models", lambda c, m, q, b: c.models(q.get("project") or None)),
    ("GET", r"/v1/models/(?P<name>[^/]+)/versions/(?P<version>[^/]+)/trace", lambda c, m, q, b: c.explain_model(m["name"], m["version"])),
    ("GET", r"/v1/runs", lambda c, m, q, b: c.runs(q.get("system") or None, _limit(q.get("limit"), 100))),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "odcl"
    dsn: str | None = None
    actor: str | None = None
    accept_lineage = False

    def log_message(self, format, *args):  # noqa: A002 - the base class names it so
        if os.environ.get("ODCL_SERVE_LOG"):
            super().log_message(format, *args)

    # ---- responses -------------------------------------------------------------------------------------------
    def _send(self, status: int, body: bytes, kind: str, extra: dict | None = None) -> None:
        self.send_response(status)
        headers = {"Content-Type": kind, "Content-Length": str(len(body)), "X-Content-Type-Options": "nosniff",
                   "Referrer-Policy": "no-referrer", "Cache-Control": "no-store", **(extra or {})}
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, value) -> None:
        self._send(status, json.dumps(value, default=str).encode(), "application/json")

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"error": {"code": code, "message": message}})

    # ---- requests --------------------------------------------------------------------------------------------
    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_GET(self):  # noqa: N802
        url = urlsplit(self.path)
        if url.path in STATIC:
            name, kind = STATIC[url.path]
            body = resources.files("odcl").joinpath("web", name).read_bytes()
            return self._send(200, body, kind, {"Content-Security-Policy": POLICY, "X-Frame-Options": "DENY"})
        self._route("GET", url)

    def do_POST(self):  # noqa: N802
        url = urlsplit(self.path)
        if url.path in ("/api/v1/lineage", "/v1/lineage"):
            return self._lineage()
        self._route("POST", url)

    def _route(self, method: str, url) -> None:
        path = url.path.rstrip("/") or "/"
        found = next(((allowed, handler, match) for allowed, pattern, handler in ROUTES if (match := re.fullmatch(pattern, path))), None)
        if found is None:
            return self._error(404, "not_found", f"Nothing at {path}")
        allowed, handler, match = found
        if method != allowed:
            return self._error(405, "method", f"{path} answers {allowed}")
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        params = {k: unquote(v) for k, v in match.groupdict().items()}
        self._answer(lambda c: handler(c, params, query, None))

    def _answer(self, operation, created: bool = False) -> None:
        try:
            with Catalog(self.dsn, actor=self.actor) as catalog:
                result = operation(catalog)
        except CatalogError as e:
            return self._error(404 if e.code in NOT_FOUND else 503 if e.code in ("database", "schema") else 400, e.code, str(e))
        status = 201 if created and isinstance(result, dict) and result.get("change") == "recorded" else 200
        self._json(status, result)

    def _lineage(self) -> None:
        if not self.accept_lineage:
            return self._error(403, "read_only", "This server does not accept OpenLineage events. Start it with odcl serve --accept-lineage")
        token = os.environ.get("ODCL_LINEAGE_TOKEN")
        if token:
            given = self.headers.get("Authorization", "")
            if not hmac.compare_digest(given.encode(), f"Bearer {token}".encode()):
                return self._error(401, "unauthorized", "Send the lineage token as Authorization: Bearer TOKEN")
        length = self.headers.get("Content-Length", "")
        if not length.isdigit() or int(length) > MAX_BODY:
            return self._error(413 if length.isdigit() else 411, "body", f"Send one event of at most {MAX_BODY} bytes with a Content-Length")
        try:
            event = json.loads(self.rfile.read(int(length)))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._error(400, "openlineage", "The body is not JSON")
        from . import openlineage
        self._answer(lambda c: openlineage.ingest(c, event), created=True)


def make_server(dsn: str | None = None, *, host: str = "127.0.0.1", port: int = 8080, actor: str | None = None,
                accept_lineage: bool = False) -> ThreadingHTTPServer:
    """A server ready to `serve_forever()`. The database is checked once now, so a wrong URL fails at start."""
    with Catalog(dsn, actor=actor):
        pass
    handler = type("OdclHandler", (Handler,), {"dsn": dsn, "actor": actor or "openlineage", "accept_lineage": accept_lineage})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server

