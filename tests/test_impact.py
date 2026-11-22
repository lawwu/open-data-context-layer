"""What each project is exposed to through the data it uses: the one read behind the overview, projects and alerts."""
import pytest

from odcl import Catalog, alerts, quality
from odcl.impact import impact


@pytest.fixture
def catalog(database):
    with Catalog(database, actor="ana") as c:
        c.put("table", {"path": "core.sales.daily_totals"})
        for name in ("amount", "day", "region"):
            c.put("column", {"table": "core.sales.daily_totals", "name": name, "dtype": "STRING"})
        c.put("project", {"name": "forecast", "description": "Daily forecast", "owner": "ana"})
        c.put("project", {"name": "board", "description": "Board pack", "owner": "ben"})
        c.use("forecast", ["core.sales.daily_totals.amount", "core.sales.daily_totals.day"])
        c.use("board", ["core.sales.daily_totals"])
        yield c


def kinds(items):
    return [(i["project"].split(":")[1], i["key"].split(".")[-1], i["kind"], i["severity"]) for i in items]


def test_one_item_per_project_entry_and_kind_of_exposure(catalog):
    assert impact(catalog) == []
    catalog.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "FLOAT64"})
    catalog._write({**catalog.history.entry("column:core.sales.daily_totals.day")["body"], "retired": True, "retired_by": "test", "links": []})
    quality.record(catalog, [{"entry": "core.sales.daily_totals.amount", "test": "not_negative", "status": "fail", "failures": 3,
                              "observed_at": "2026-01-01T00:00:00Z"}], "dbt")
    found = impact(catalog)
    assert kinds(found) == [("board", "daily_totals", "changed", "breaking"), ("board", "daily_totals", "failing", "breaking"),
                            ("forecast", "amount", "changed", "breaking"), ("forecast", "amount", "failing", "breaking"),
                            ("forecast", "day", "retired", "breaking")]
    board = found[1]
    assert board["owner"] == "ben" and board["failing"] == [{"entry": "column:core.sales.daily_totals.amount", "test": "not_negative", "status": "fail",
                                                              "failures": 3, "source": "dbt"}]
    assert any(c["change"] == "type changed" for c in found[2]["changes"])
    assert kinds(impact(catalog, "forecast", kinds=("retired",))) == [("forecast", "day", "retired", "breaking")]


def test_the_overview_a_project_and_the_project_list_all_read_it(catalog):
    catalog.put("column", {"table": "core.sales.daily_totals", "name": "amount", "dtype": "FLOAT64"})
    quality.record(catalog, [{"entry": "core.sales.daily_totals.region", "test": "known_region", "status": "error", "observed_at": "2026-01-01T00:00:00Z"}], "dbt")
    overview = catalog.overview()
    assert overview["changed_since_used"] == [{"project": "project:board", "key": "table:core.sales.daily_totals", "severity": "breaking"},
                                              {"project": "project:forecast", "key": "column:core.sales.daily_totals.amount", "severity": "breaking"}]
    assert overview["failing_and_used"] == [{"project": "project:board", "key": "table:core.sales.daily_totals",
                                             "failing": "column:core.sales.daily_totals.region", "test": "known_region"}]
    assert [(u["key"].split(".")[-1], u["changed_since"], u["severity"]) for u in catalog.get("forecast")["uses"]] == [
        ("amount", True, "breaking"), ("day", False, None)]
    # the project list counts a used table whose column changed, as the overview lists it
    assert {p["title"]: p["changed_since_used"] for p in catalog.projects()} == {"board": 1, "forecast": 1}
    before = catalog.history.revisions("project:forecast")[0]["id"]  # before it used anything
    assert catalog.uses("forecast", revision=before)["uses"] == []


def test_alerts_tell_of_a_failing_test_once_and_again_when_it_fails_anew(catalog, monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "_post", lambda url, payload: sent.append(payload))
    alerts.subscribe(catalog, project="forecast", channel="webhook", address="https://hooks.example.com/x")
    result = {"entry": "core.sales.daily_totals.amount", "test": "not_negative", "status": "fail", "failures": 3}
    quality.record(catalog, [{**result, "observed_at": "2026-01-01T00:00:00Z", "run": "a"}], "dbt")
    assert [a["kind"] for m in alerts.send(catalog)["sent"] for a in m["alerts"]] == ["failing"]
    assert sent[0]["alerts"][0]["failing"][0]["test"] == "not_negative" and "is failing a test" in sent[0]["text"] and "not_negative fail (3 failing)" in sent[0]["text"]
    assert alerts.send(catalog)["sent"] == []
    quality.record(catalog, [{**result, "status": "pass", "failures": 0, "observed_at": "2026-01-02T00:00:00Z", "run": "b"}], "dbt")
    assert alerts.send(catalog)["sent"] == []  # passing again: nothing to tell
    quality.record(catalog, [{**result, "observed_at": "2026-01-03T00:00:00Z", "run": "c"}], "dbt")
    assert len(alerts.send(catalog)["sent"]) == 1  # failing anew is told again
