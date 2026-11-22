"""Alerts: who is told when data a project uses changes after it used it, or is gone from its source."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from odcl import Catalog, CatalogError, alerts
from odcl.cli import main


@pytest.fixture
def hook(monkeypatch):
    """A local webhook that keeps what is posted to it, and fails while `failing` is set. It is on this machine, an
    internal address, so it is allowed the way an admin would allow one."""
    monkeypatch.setenv("ODCL_ALERT_ALLOW_HOSTS", "127.0.0.1")
    got, state = [], {"failing": False}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if state["failing"]:
                self.send_response(500)
            else:
                got.append(body)
                self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/hook", got, state
    server.shutdown()


@pytest.fixture
def catalog(database):
    with Catalog(database, actor="ana") as c:
        c.put("table", {"path": "core.sales.daily_totals"})
        c.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "NUMERIC"})
        c.put("column", {"table": "core.sales.daily_totals", "name": "day", "dtype": "DATE"})
        c.put("project", {"name": "forecast", "description": "Daily forecast", "owner": "ana"})
        c.put("project", {"name": "board", "description": "Board pack", "owner": "ben"})
        c.use("forecast", ["core.sales.daily_totals.amount"])
        c.use("board", ["core.sales.daily_totals"])
        yield c


def test_each_change_is_sent_once_to_the_subscriptions_that_cover_it(catalog, hook):
    url, got, _ = hook
    alerts.subscribe(catalog, project="forecast", channel="webhook", address=url)
    alerts.subscribe(catalog, owner="BEN", channel="slack", address=url, min_severity="breaking")
    assert alerts.send(catalog)["sent"] == []  # nothing changed yet

    catalog.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "NUMERIC", "description": "Net"})  # cosmetic
    assert alerts.send(catalog)["sent"] == []
    catalog.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "FLOAT64"})  # breaking
    result = alerts.send(catalog)
    assert [(m["subscription"], [a["project"] for a in m["alerts"]]) for m in result["sent"]] == [(1, ["project:forecast"]), (2, ["project:board"])]
    webhook, slack = got
    assert webhook["alerts"][0]["severity"] == "breaking" and webhook["alerts"][0]["key"] == "column:core.sales.daily_totals.amount"
    assert any(c["change"] == "type changed" for c in webhook["alerts"][0]["changes"])
    assert "board uses core.sales.daily_totals, which changed: breaking" in slack["text"] and "NUMERIC -> FLOAT64" in slack["text"]
    assert alerts.send(catalog)["sent"] == [] and len(got) == 2  # sent once

    catalog.use("forecast", ["core.sales.daily_totals.amount"])  # the project has seen the new definition
    catalog._write({**catalog._head("column:core.sales.daily_totals.day")["body"], "retired": True, "retired_by": "test", "links": []})
    again = alerts.send(catalog)
    assert [(a["project"], a["kind"], a["severity"]) for m in again["sent"] for a in m["alerts"]] == [("project:board", "changed", "breaking")]
    catalog.use("forecast", ["core.sales.daily_totals.day"])
    catalog._write({**catalog._head("column:core.sales.daily_totals.amount")["body"], "retired": True, "retired_by": "test", "links": []})
    gone = [(a["project"], a["key"], a["kind"]) for m in alerts.send(catalog)["sent"] for a in m["alerts"]]
    assert ("project:forecast", "column:core.sales.daily_totals.amount", "retired") in gone


def test_a_failed_delivery_is_tried_again_and_a_dry_run_sends_nothing(catalog, hook):
    url, got, state = hook
    alerts.subscribe(catalog, everything=True, channel="webhook", address=url)
    catalog.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "FLOAT64"})
    dry = alerts.send(catalog, dry_run=True)
    assert dry["dry_run"] and len(dry["sent"][0]["alerts"]) == 2 and "forecast uses" in dry["sent"][0]["text"] and got == []
    state["failing"] = True
    failed = alerts.send(catalog)
    assert failed["sent"] == [] and "500" in failed["failed"][0]["error"]
    state["failing"] = False
    assert len(alerts.send(catalog)["sent"]) == 1 and len(got) == 1


def test_a_delivery_that_keeps_failing_waits_longer_says_so_and_recovers(catalog, hook, database, capsys):
    url, got, state = hook
    made = alerts.subscribe(catalog, everything=True, channel="webhook", address=url)
    assert alerts.subscriptions(catalog)[0]["delivery"] == {"status": "ok"}
    catalog.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "FLOAT64"})
    state["failing"] = True
    for _ in range(alerts.RETRY_EVERY_RUN):  # the first tries are made on every run
        assert len(alerts.send(catalog)["failed"]) == 1
    delivery = alerts.subscriptions(catalog)[0]["delivery"]
    assert delivery["status"] == "failing" and delivery["attempts"] == alerts.RETRY_EVERY_RUN and "500" in delivery["last_error"]
    assert delivery["next_attempt"] is not None  # then it waits an hour

    waited = alerts.send(catalog)
    assert waited["failed"] == [] and waited["deferred"][0]["subscription"] == made["id"] and waited["deferred"][0]["next_attempt"] == delivery["next_attempt"]
    assert main(["--database-url", database, "alerts", "list"]) == 0 and "FAILING since" in capsys.readouterr().out

    catalog.db.execute("UPDATE alert_deliveries SET attempted_at = attempted_at - interval '61 minutes'")
    assert len(alerts.send(catalog)["failed"]) == 1  # tried after the hour, and now waits two
    assert alerts.subscriptions(catalog)[0]["delivery"]["attempts"] == alerts.RETRY_EVERY_RUN + 1
    assert alerts.send(catalog)["deferred"]
    catalog.db.execute("UPDATE alert_deliveries SET attempted_at = attempted_at - interval '121 minutes'")
    state["failing"] = False
    assert len(alerts.send(catalog)["sent"]) == 1 and len(got) == 1
    assert alerts.subscriptions(catalog)[0]["delivery"] == {"status": "ok"}


def test_what_was_owed_about_something_no_longer_there_is_dropped(catalog, hook):
    url, got, state = hook
    alerts.subscribe(catalog, project="forecast", channel="webhook", address=url)
    catalog.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "FLOAT64"})
    state["failing"] = True
    assert alerts.send(catalog)["failed"]
    catalog.use("forecast", ["core.sales.daily_totals.amount"])  # the project has seen the change: nothing left to tell
    assert alerts.send(catalog) == {"sent": [], "failed": [], "deferred": [], "dry_run": False}
    assert alerts.subscriptions(catalog)[0]["delivery"] == {"status": "ok"}


def test_subscriptions_are_checked_masked_and_kept_to_their_owner(catalog, database, capsys):
    with pytest.raises(CatalogError, match="https"):
        alerts.subscribe(catalog, everything=True, channel="slack", address="http://hooks.example.com/services/T/B/secret")
    with pytest.raises(CatalogError, match="one project"):
        alerts.subscribe(catalog, project="forecast", everything=True, channel="email", address="a@example.com")
    made = alerts.subscribe(catalog, everything=True, channel="slack", address="https://hooks.example.com/services/T/B/secret")
    assert made["address"] == "https://hooks.example.com/…"
    assert main(["--database-url", database, "alerts", "subscribe", "--project", "forecast", "--email", "ana@example.com"]) == 0
    assert main(["--database-url", database, "alerts", "list"]) == 0
    out = capsys.readouterr().out
    assert "secret" not in out and "ana@example.com" in out
    assert main(["--database-url", database, "alerts", "remove", str(made["id"])]) == 0
    assert [s["channel"] for s in alerts.subscriptions(catalog)] == ["email"]


def test_email_goes_through_the_configured_smtp_server(catalog, monkeypatch):
    sent = []

    class SMTP:
        def __init__(self, host, port, timeout):
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def starttls(self):
            sent.append(("tls",))

        def login(self, user, password):
            sent.append(("login", user))

        def send_message(self, message):
            sent.append(("message", message["To"], message["Subject"]))

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", SMTP)
    monkeypatch.setenv("ODCL_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("ODCL_SMTP_FROM", "odcl@example.com")
    monkeypatch.setenv("ODCL_SMTP_USER", "odcl")
    alerts.subscribe(catalog, project="forecast", channel="email", address="ana@example.com")
    catalog.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "FLOAT64"})
    assert len(alerts.send(catalog)["sent"]) == 1
    assert sent == [("connect", "smtp.example.com", 587), ("tls",), ("login", "odcl"),
                    ("message", "ana@example.com", "odcl: 1 change to data your projects use")]
