"""Alerts: tell the people behind a project when data it uses changes after it used it, or is gone from its source.

A subscription covers one project, every project of one owner, or every project, and sends to a webhook (JSON), a
Slack incoming webhook or an email address, for changes at or above a severity (see `odcl.changes`). `send` finds
what is new since the last run and delivers it, one message per subscription; run it after imports, from the same
scheduler. Each change is sent once per subscription: a delivery is recorded for the project, the entry and the
entry's state (its newest revision, or its newest column's for a table). A failed delivery is tried again next run.

Email goes through the SMTP server named by $ODCL_SMTP_HOST ($ODCL_SMTP_PORT, $ODCL_SMTP_USER, $ODCL_SMTP_PASSWORD,
$ODCL_SMTP_FROM). With $ODCL_PUBLIC_URL, each alert links to the entry's page.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request

from .changes import SEVERITY
from .impact import impact
from .store import Catalog, CatalogError, require

CHANNELS = ("webhook", "slack", "email")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SHOWN = 5  # changes listed per alert in a message; the webhook's JSON has them all


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
        parts = urllib.parse.urlsplit(address)
        address = f"{parts.scheme}://{parts.netloc}/…"
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
    if catalog.user is None or catalog.user["role"] == "admin":
        return "TRUE", []
    return "created_by = %s", [catalog.actor]


def subscriptions(catalog: Catalog) -> list[dict]:
    where, params = _mine(catalog)
    return [_masked(r) for r in catalog.db.execute(f"SELECT * FROM alert_subscriptions WHERE {where} ORDER BY id", params)]


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


def _post(url: str, payload: dict) -> None:
    request = urllib.request.Request(url, method="POST", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - the scheme is checked when subscribing
        require(200 <= response.status < 300, "delivery", f"{response.status} from the webhook")


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
    """Deliver what each subscription has not been sent yet. With `dry_run`, say what would be sent and send nothing."""
    catalog.allow("importer")
    db, found = catalog.db, findings(catalog)
    out = {"sent": [], "failed": [], "dry_run": dry_run}
    for s in db.execute("SELECT * FROM alert_subscriptions ORDER BY id").fetchall():
        sent = {(r["project_key"], r["entry_key"], r["entry_state"]) for r in db.execute(
            "SELECT project_key, entry_key, entry_state FROM alert_deliveries WHERE subscription_id = %s AND sent_at IS NOT NULL", (s["id"],))}
        new = [f for f in found if _covers(s, f) and (f["project"], f["key"], f["state"]) not in sent]
        if not new:
            continue
        summary = {"subscription": s["id"], "channel": s["channel"], "alerts": [{k: f[k] for k in ("project", "key", "kind", "severity")} for f in new]}
        if dry_run:
            out["sent"].append({**summary, "text": _text(new)})
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
                                attempted_at = now(), sent_at = excluded.sent_at, error = excluded.error""",
                           (s["id"], f["project"], f["key"], f["state"], f["kind"], f["severity"], error, error))
        (out["failed"] if error else out["sent"]).append({**summary, **({"error": error} if error else {})})
    return out
