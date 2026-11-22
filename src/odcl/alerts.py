"""Alerts: tell the people behind a project when data it uses changes after it used it, or is gone from its source.

A subscription covers one project, every project of one owner, or every project, and sends to a webhook (JSON), a
Slack incoming webhook or an email address, for changes at or above a severity (see `odcl.changes`). `send` finds
what is new since the last run and delivers it, one message per subscription; run it after imports, from the same
scheduler. Each change is sent once per subscription: a delivery is recorded for the project, the entry and the
entry's state (its newest revision, or its newest column's for a table).

A delivery that fails is tried again on each of the next runs; after RETRY_EVERY_RUN failed tries in a row it waits
between runs, an hour and then twice as long each time, up to a day. Until a message gets through, the subscription's
`delivery` says it is failing: since when, how many tries, the last error, and when it is tried next. A delivery still
owed about something that is no longer there to tell (the entry changed again, the test passes again) is dropped.

Email goes through the SMTP server named by $ODCL_SMTP_HOST ($ODCL_SMTP_PORT, $ODCL_SMTP_USER, $ODCL_SMTP_PASSWORD,
$ODCL_SMTP_FROM). With $ODCL_PUBLIC_URL, each alert links to the entry's page.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from .changes import SEVERITY
from .impact import impact
from .store import Catalog, CatalogError, require, stamp

CHANNELS = ("webhook", "slack", "email")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SHOWN = 5  # changes listed per alert in a message; the webhook's JSON has them all
RETRY_EVERY_RUN = 3  # failed tries in a row before a delivery waits between runs
WAIT_FIRST, WAIT_MOST = timedelta(hours=1), timedelta(hours=24)  # the wait doubles with each further failure


def _address(channel: str, address: str) -> str:
    require(isinstance(address, str) and address.strip(), "address", f"Give the {channel} address")
    address = address.strip()
    if channel == "email":
        require(EMAIL.match(address), "address", f"{address} is not an email address")
        return address
    parts = urllib.parse.urlsplit(address)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    require(parts.scheme == "https" or (parts.scheme == "http" and local), "address",
            "A webhook is an https:// address (or http:// on this machine), so what is sent is not sent in the clear")
    return address


def _masked(row: dict) -> dict:
    """A subscription as it is shown: a webhook's path, which is often its secret, is left out."""
    address = row["address"]
    if row["channel"] != "email":
        parts = urllib.parse.urlsplit(address)  # the host only: a path, or a user:password@, is often the secret
        address = f"{parts.scheme}://{parts.hostname}{f':{parts.port}' if parts.port else ''}/…"
    return {"id": row["id"], "scope": row["scope"], "target": row["target"], "channel": row["channel"], "address": address,
            "min_severity": row["min_severity"], "created_by": row["created_by"]}


def subscribe(catalog: Catalog, *, project: str | None = None, owner: str | None = None, everything: bool = False,
              channel: str, address: str, min_severity: str = "semantic") -> dict:
    """Send alerts about one project, the projects one owner owns, or every project (`everything`)."""
    catalog.allow("editor")
    require(sum(map(bool, (project, owner, everything))) == 1, "scope", "Subscribe to one project, one owner's projects, or every project")
    require(channel in CHANNELS, "channel", f"A channel is one of: {', '.join(CHANNELS)}")
    require(min_severity in SEVERITY, "min_severity", f"A severity is one of: {', '.join(SEVERITY)}")
    scope, target = ("project", catalog.resolve(project, ("project",))) if project else ("owner", owner.strip()) if owner else ("all", None)
    row = catalog.db.execute("""INSERT INTO alert_subscriptions (scope, target, channel, address, min_severity, created_by)
                                VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
                             (scope, target, channel, _address(channel, address), min_severity, catalog.actor)).fetchone()
    return _masked(row)


def _mine(catalog: Catalog) -> tuple[str, list]:
    """Which subscriptions the caller may see and remove: their own, or every one for an admin or a direct connection."""
    if catalog.user is None and getattr(catalog, "anonymous", False):
        return "FALSE", []  # someone who did not sign in sees no one's
    if catalog.user is None or catalog.user["role"] == "admin":
        return "TRUE", []
    return "created_by = %s", [catalog.actor]


def _delivery(db, subscription_id: int) -> tuple[dict, bool]:
    """Whether a subscription's messages are getting through, and whether `send` waits before trying it again.

    `ok`, or `failing` while any delivery is still owed: `since` the first failed try, `attempts` (the most tries of
    one delivery), the `last_error`, and `next_attempt`: a time, or None for the next run."""
    row = db.execute("""
        SELECT count(*) AS owed, max(attempts) AS attempts, min(first_attempted_at) AS since, max(attempted_at) AS last,
               (array_agg(error ORDER BY attempted_at DESC))[1] AS error,
               CASE WHEN max(attempts) >= %(every_run)s
                    THEN max(attempted_at) + LEAST(%(first)s * power(2, max(attempts) - %(every_run)s), %(most)s) END AS next_attempt,
               now() AS now
        FROM alert_deliveries WHERE subscription_id = %(id)s AND sent_at IS NULL""",
                     {"id": subscription_id, "every_run": RETRY_EVERY_RUN, "first": WAIT_FIRST, "most": WAIT_MOST}).fetchone()
    if not row["owed"]:
        return {"status": "ok"}, False
    waiting = row["next_attempt"] is not None and row["next_attempt"] > row["now"]
    return {"status": "failing", "since": stamp(row["since"]), "attempts": row["attempts"], "last_attempt": stamp(row["last"]),
            "last_error": row["error"], "next_attempt": stamp(row["next_attempt"]) if waiting else None}, waiting


def subscriptions(catalog: Catalog) -> list[dict]:
    """The caller's subscriptions (see `_mine`), each with its `delivery`: whether its messages are getting through."""
    where, params = _mine(catalog)
    return [{**_masked(r), "delivery": _delivery(catalog.db, r["id"])[0]}
            for r in catalog.db.execute(f"SELECT * FROM alert_subscriptions WHERE {where} ORDER BY id", params)]


def unsubscribe(catalog: Catalog, subscription: int) -> dict:
    catalog.allow("editor")
    where, params = _mine(catalog)
    row = catalog.db.execute(f"DELETE FROM alert_subscriptions WHERE id = %s AND {where} RETURNING *", [int(subscription), *params]).fetchone()
    require(row, "not_found", f"No subscription {subscription} of yours")
    return {"removed": _masked(row)}


def findings(catalog: Catalog) -> list[dict]:
    """What there is to tell people: every exposure of every project that has a severity. See `odcl.impact`."""
    return [i for i in impact(catalog) if i["severity"]]


def _covers(subscription: dict, finding: dict) -> bool:
    if SEVERITY.index(finding["severity"]) < SEVERITY.index(subscription["min_severity"]):
        return False
    if subscription["scope"] == "project":
        return finding["project"] == subscription["target"]
    if subscription["scope"] == "owner":
        return (finding["owner"] or "").strip().lower() == subscription["target"].lower()
    return True


def _link(key: str) -> str | None:
    base = os.environ.get("ODCL_PUBLIC_URL")
    return f"{base.rstrip('/')}/#/entry/{urllib.parse.quote(key, safe='')}" if base else None


def _text(findings: list[dict]) -> str:
    lines = [f"{len(findings)} change{'s' if len(findings) != 1 else ''} to data your projects use, after they used it:", ""]
    for f in findings:
        what = {"retired": "is gone from its source", "changed": "changed", "failing": "is failing a test"}[f["kind"]]
        lines.append(f"- {f['project'].removeprefix('project:')} uses {f['key'].split(':', 1)[1]}, which {what}: {f['severity']}")
        for t in f.get("failing", [])[:SHOWN]:
            lines.append(f"    {t['entry'].split(':', 1)[1]}: {t['test']} {t['status']}" + (f" ({t['failures']} failing)" if t["failures"] else "") + f" [{t['source']}]")
        for c in f["changes"][:SHOWN]:
            shown = lambda v: "(none)" if v in (None, "") else v
            detail = "" if (c.get("before") is None and c.get("after") is None) or isinstance(c.get("after"), list) else f" ({shown(c['before'])} -> {shown(c['after'])})"
            lines.append(f"    {c['key'].split(':', 1)[1]}: {c['change']}{detail} [{c['severity']}]")
        if len(f["changes"]) > SHOWN:
            lines.append(f"    and {len(f['changes']) - SHOWN} more")
        if _link(f["key"]):
            lines.append(f"    {_link(f['key'])}")
    return "\n".join(lines)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a webhook that redirects is a failed delivery: following it could reach anywhere


def _reachable(url: str) -> None:
    """Refuse a webhook on an internal address (loopback, private, link-local such as a cloud's metadata service), unless
    its host is in $ODCL_ALERT_ALLOW_HOSTS. Checked when sending, since what a name resolves to can change."""
    import ipaddress
    import socket
    parts = urllib.parse.urlsplit(url)
    require(parts.scheme in ("https", "http") and parts.hostname, "delivery", "The webhook's address is not a web address")
    allowed = {h.strip().lower() for h in os.environ.get("ODCL_ALERT_ALLOW_HOSTS", "").split(",") if h.strip()}
    if parts.hostname.lower() in allowed:
        return
    require(parts.scheme == "https", "delivery", "A webhook is sent only over https://")
    try:
        found = {info[4][0] for info in socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)}
    except OSError:
        raise CatalogError("delivery", f"Could not find {parts.hostname}") from None
    inside = [a for a in found if not ipaddress.ip_address(a.split("%")[0]).is_global]
    require(not inside, "delivery", f"{parts.hostname} is an internal address; an admin can allow it with ODCL_ALERT_ALLOW_HOSTS")


def _post(url: str, payload: dict) -> None:
    _reachable(url)
    request = urllib.request.Request(url, method="POST", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(_NoRedirects).open(request, timeout=10) as response:  # noqa: S310 - checked above
            require(200 <= response.status < 300, "delivery", f"The webhook answered {response.status}")
    except urllib.error.HTTPError as e:
        raise CatalogError("delivery", f"The webhook answered {e.code}") from None
    except (urllib.error.URLError, OSError):
        raise CatalogError("delivery", "Could not reach the webhook") from None


def _email(address: str, subject: str, text: str) -> None:
    import smtplib
    from email.message import EmailMessage
    host = os.environ.get("ODCL_SMTP_HOST")
    require(host and os.environ.get("ODCL_SMTP_FROM"), "delivery", "Email alerts need $ODCL_SMTP_HOST and $ODCL_SMTP_FROM")
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = os.environ["ODCL_SMTP_FROM"], address, subject
    message.set_content(text)
    port = int(os.environ.get("ODCL_SMTP_PORT") or 587)
    with smtplib.SMTP(host, port, timeout=10) as smtp:
        if port != 25:
            smtp.starttls()
        if os.environ.get("ODCL_SMTP_USER"):
            smtp.login(os.environ["ODCL_SMTP_USER"], os.environ.get("ODCL_SMTP_PASSWORD") or "")
        smtp.send_message(message)


def deliver(subscription: dict, found: list[dict]) -> None:
    """Send one message with these findings over the subscription's channel. Raises on failure."""
    text = _text(found)
    if subscription["channel"] == "slack":
        _post(subscription["address"], {"text": text})
    elif subscription["channel"] == "webhook":
        said = ("project", "key", "kind", "severity", "changes", "failing")
        _post(subscription["address"], {"source": "odcl", "text": text, "alerts": [{**{k: f[k] for k in said if k in f}, "url": _link(f["key"])} for f in found]})
    else:
        _email(subscription["address"], f"odcl: {len(found)} change{'s' if len(found) != 1 else ''} to data your projects use", text)


def send(catalog: Catalog, dry_run: bool = False) -> dict:
    """Deliver what each subscription has not been sent yet. With `dry_run`, say what would be sent and send nothing.

    Says what was `sent`, what `failed`, and which subscriptions were `deferred`: failing, and waiting before the next try."""
    catalog.allow("importer")
    db, found = catalog.db, findings(catalog)
    out = {"sent": [], "failed": [], "deferred": [], "dry_run": dry_run}
    for s in db.execute("SELECT * FROM alert_subscriptions ORDER BY id").fetchall():
        sent = {(r["project_key"], r["entry_key"], r["entry_state"]) for r in db.execute(
            "SELECT project_key, entry_key, entry_state FROM alert_deliveries WHERE subscription_id = %s AND sent_at IS NOT NULL", (s["id"],))}
        new = [f for f in found if _covers(s, f) and (f["project"], f["key"], f["state"]) not in sent]
        if not dry_run:  # what was owed about something no longer there to tell is owed no more
            owing = {(f["project"], f["key"], f["state"]) for f in new}
            for r in db.execute("SELECT project_key, entry_key, entry_state FROM alert_deliveries WHERE subscription_id = %s AND sent_at IS NULL",
                                (s["id"],)).fetchall():
                if (r["project_key"], r["entry_key"], r["entry_state"]) not in owing:
                    db.execute("DELETE FROM alert_deliveries WHERE subscription_id = %s AND project_key = %s AND entry_key = %s AND entry_state = %s",
                               (s["id"], r["project_key"], r["entry_key"], r["entry_state"]))
        if not new:
            continue
        summary = {"subscription": s["id"], "channel": s["channel"], "alerts": [{k: f[k] for k in ("project", "key", "kind", "severity")} for f in new]}
        if dry_run:
            out["sent"].append({**summary, "text": _text(new)})
            continue
        delivery, waiting = _delivery(db, s["id"])
        if waiting:
            out["deferred"].append({**summary, "attempts": delivery["attempts"], "last_error": delivery["last_error"],
                                    "next_attempt": delivery["next_attempt"]})
            continue
        error = None
        try:
            deliver(s, new)
        except (CatalogError, OSError, ValueError) as e:  # urllib's HTTPError and URLError are OSErrors
            error = str(e)[:500] or type(e).__name__
        with db.transaction():
            for f in new:
                db.execute("""INSERT INTO alert_deliveries (subscription_id, project_key, entry_key, entry_state, kind, severity, sent_at, error)
                              VALUES (%s, %s, %s, %s, %s, %s, CASE WHEN %s::text IS NULL THEN now() END, %s)
                              ON CONFLICT (subscription_id, project_key, entry_key, entry_state) DO UPDATE SET
                                attempted_at = now(), attempts = alert_deliveries.attempts + 1, sent_at = excluded.sent_at, error = excluded.error""",
                           (s["id"], f["project"], f["key"], f["state"], f["kind"], f["severity"], error, error))
        (out["failed"] if error else out["sent"]).append({**summary, **({"error": error} if error else {})})
    return out
