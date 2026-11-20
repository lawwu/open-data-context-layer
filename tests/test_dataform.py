"""A Dataform build: its compiled definitions, its run, the BigQuery job that ran it, and the snapshot it handed on.

Everything here reads the staffing trace fixture. `expected_explain.json` is the independent statement of what the
trace should say; these tests check the dataset half of it.
"""
import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from odcl import Catalog, CatalogError, builds, dataform

FIXTURES = Path(__file__).parent / "fixtures" / "staffing_trace"
EXPECTED = json.loads((FIXTURES / "expected_explain.json").read_text())
INVOCATION = "projects/workforce-demo/locations/us-central1/repositories/workforce/workflowInvocations/build-042"
TARGET = "workforce-demo.features.shift_inputs"
SNAPSHOT = "workforce-demo.frozen.shift_inputs_b042"
RAW = "workforce-demo.raw.shift_totals"


def load(name):
    return json.loads((FIXTURES / name).read_text())


class FakeDataform:
    """Answers the four Dataform reads from the fixture, as the real client would after paging."""

    def __init__(self, invocation=None, compilation=None):
        self.invocation = invocation or load("dataform_workflow_invocation.json")
        self.compilation = compilation or load("dataform_compilation_result.json")

    def get(self, name):
        if name == self.invocation["workflowInvocation"]["name"]:
            return self.invocation["workflowInvocation"]
        assert name == self.compilation["compilationResult"]["name"], name
        return self.compilation["compilationResult"]

    def query(self, name, field):
        source = self.invocation if "workflowInvocations" in name else self.compilation
        return source[field]


def snapshot_facts(base=TARGET, time="2026-09-02T02:00:00Z", extra_fields=()):
    table = next(t for t in load("bigquery_tables.json") if t["type"] == "SNAPSHOT")
    return {"table": SNAPSHOT, "kind": "SNAPSHOT", "base_table": base, "snapshot_time": time,
            "schema": [f["name"] for f in table["schema"]["fields"]] + list(extra_fields)}


def sync_history(catalog, extra=()):
    """Read the fixture's BigQuery job history into the catalog the way `odcl sync bigquery-jobs` does."""
    pytest.importorskip("sounding", reason="query history is read through Sounding, which needs Python 3.14")
    from types import SimpleNamespace as NS

    from google.cloud import bigquery as gbq

    from odcl import bigquery as bq

    class Page(list):
        _raw_page = {}

    jobs = [gbq.QueryJob.from_api_repr(r, Mock()) for r in [*load("bigquery_jobs.json"), *extra]]
    client = Mock(list_jobs=Mock(side_effect=lambda **k: NS(pages=iter([Page(jobs)]))))
    from datetime import datetime, timezone
    return bq.sync_jobs(catalog, "workforce-demo", client=client, until=datetime(2026, 9, 3, tzinfo=timezone.utc))


@pytest.fixture
def catalog(database):
    with Catalog(database, actor="ana") as c:
        yield c


def test_an_invocation_registers_its_definitions_and_records_its_runs(catalog):
    result = dataform.import_invocation(catalog, INVOCATION, client=FakeDataform())
    assert result["entries"] == {"added": 8, "changed": 0, "unchanged": 0}  # two tables and their six columns
    assert [(a["target"], a["state"], a["job_id"]) for a in result["actions"]] == [(TARGET, "SUCCEEDED", "job-042")]

    table = catalog.get(TARGET)
    assert table["source"]["dataform"]["file"] == "definitions/shift_inputs.sqlx" and table["type"] == "table"
    assert table["description"].startswith("Hours worked per employee") and [t["key"] for t in table["derived_from"]] == [f"table:{RAW}"]
    hours = catalog.get(f"{TARGET}.hours_at_cutoff")
    assert hours["definition"] == "worked_minutes_asof / 60" and hours["tags"] == []
    assert [c["key"] for c in hours["derived_from"]] == [f"column:{RAW}.worked_minutes_asof"]
    assert catalog.get(RAW)["description"] == "Shift totals as loaded from timekeeping"  # a declaration is a table too

    again = dataform.import_invocation(catalog, INVOCATION, client=FakeDataform())
    assert again["entries"] == {"added": 0, "changed": 0, "unchanged": 5}  # the raw columns it reads are already there
    assert again["actions"] == result["actions"]


def test_bigquery_and_dataform_describe_the_same_columns_once(catalog):
    pytest.importorskip("sounding", reason="the BigQuery importer needs Sounding, which needs Python 3.14")
    from google.cloud import bigquery as gbq

    from odcl import bigquery as bq
    tables = {t["tableReference"]["datasetId"] + "." + t["tableReference"]["tableId"]: gbq.Table.from_api_repr(t) for t in load("bigquery_tables.json")}
    client = Mock(get_table=Mock(side_effect=lambda ref: tables[f"{ref.dataset_id}.{ref.table_id}"]))

    bq.import_tables(catalog, [RAW, TARGET], client=client)
    before = catalog.overview()["counts"]
    dataform.import_invocation(catalog, INVOCATION, client=FakeDataform())
    assert catalog.overview()["counts"] == before == {"table": 2, "column": 6, "project": 0}  # no new keys: the same physical columns
    hours = catalog.get(f"{TARGET}.hours_at_cutoff")
    assert (hours["dtype"], hours["definition"]) == ("FLOAT", "worked_minutes_asof / 60")  # BigQuery's type, Dataform's definition
    assert sorted(hours["source"]) == ["bigquery", "dataform"]
    assert bq.import_tables(catalog, [RAW, TARGET], client=client) == {"added": 0, "changed": 0, "unchanged": 8}


def test_a_handoff_binds_the_snapshot_to_its_build_and_the_trace_reads_back(catalog):
    sync_history(catalog)
    dataform.import_invocation(catalog, INVOCATION, client=FakeDataform())
    recorded = builds.record_handoff(catalog, INVOCATION, TARGET, snapshot_facts())
    assert recorded["reference"] == EXPECTED["read"]["dataset_version"] and recorded["change"] == "added"
    assert builds.record_handoff(catalog, INVOCATION, TARGET, snapshot_facts())["change"] == "unchanged"  # a replay is a no-op

    trace = builds.explain_dataset(catalog, SNAPSHOT)
    read, wanted = EXPECTED["read"], EXPECTED["inputs"][0]
    assert (trace["reference"], trace["snapshot"], trace["snapshot_of"], trace["snapshot_time"]) == (
        read["dataset_version"], read["snapshot"], read["snapshot_of"], read["snapshot_time"])
    assert {k: trace["produced_by"][k] for k in wanted["produced_by"]} == wanted["produced_by"]
    assert {k: trace["executed_as"][k] for k in wanted["executed_as"]} == wanted["executed_as"]
    assert trace["defined_in"] == wanted["defined_in"]
    column = next(c for c in trace["columns"] if c["field"] == wanted["field"])
    assert (column["column"], column["expression"], [d["column"] for d in column["derived_from"]], column["lineage"]) == (
        wanted["column"], wanted["expression"], wanted["derived_from"], wanted["lineage"])
    assert trace["checks"]["written_again_before_snapshot"] is False and trace["missing"] == []


def test_the_trace_is_the_same_whether_job_history_is_read_before_or_after_the_build(database):
    traces = []
    for history_first in (True, False):
        with Catalog(database, actor="ana") as catalog:
            catalog.db.execute("TRUNCATE entries, revisions, revision_links, query_jobs, query_tables, query_patterns, sync_cursors, "
                               "executions, dataset_versions, version_columns CASCADE")
            if history_first:
                sync_history(catalog)
            dataform.import_invocation(catalog, INVOCATION, client=FakeDataform())
            builds.record_handoff(catalog, INVOCATION, TARGET, snapshot_facts())
            if not history_first:
                assert "job job-042 is not in the synced query history of workforce-demo" in builds.explain_dataset(catalog, SNAPSHOT)["missing"]
                sync_history(catalog)
            trace = builds.explain_dataset(catalog, SNAPSHOT)
            traces.append(json.loads(json.dumps(trace, default=str)))
    strip = lambda t: {k: v for k, v in t.items() if k not in ("columns", "definition_revision", "recorded_at")}
    assert strip(traces[0]) == strip(traces[1])
    assert [(c["field"], c["expression"], c["lineage"]) for c in traces[0]["columns"]] == [(c["field"], c["expression"], c["lineage"]) for c in traces[1]["columns"]]


def test_a_skipped_action_hands_on_nothing(catalog):
    invocation = load("dataform_workflow_invocation.json")
    invocation["workflowInvocationActions"][0] |= {"state": "SKIPPED"}
    del invocation["workflowInvocationActions"][0]["bigqueryAction"]
    result = dataform.import_invocation(catalog, INVOCATION, client=FakeDataform(invocation=invocation))
    assert [(a["state"], a["job_id"]) for a in result["actions"]] == [("SKIPPED", None)]
    with pytest.raises(CatalogError) as error:
        builds.record_handoff(catalog, INVOCATION, TARGET, snapshot_facts())
    assert error.value.code == "not_succeeded"


def test_a_snapshot_must_copy_the_target_after_the_build_and_before_another_write(catalog):
    dataform.import_invocation(catalog, INVOCATION, client=FakeDataform())
    for facts, code in ((snapshot_facts(base=RAW), "snapshot_of_other_table"), (snapshot_facts(time="2026-09-02T01:59:00Z"), "snapshot_before_build")):
        with pytest.raises(CatalogError) as error:
            builds.record_handoff(catalog, INVOCATION, TARGET, facts)
        assert error.value.code == code

    later = copy.deepcopy(load("bigquery_jobs.json")[0])
    later["jobReference"]["jobId"] = "someone-else"
    later["statistics"] |= {"creationTime": "1788314398000", "startTime": "1788314398000", "endTime": "1788314399000"}
    sync_history(catalog, extra=[later])
    with pytest.raises(CatalogError) as error:
        builds.record_handoff(catalog, INVOCATION, TARGET, snapshot_facts())
    assert error.value.code == "written_again" and "someone-else" in str(error.value)
    assert catalog.db.execute("SELECT count(*) AS n FROM dataset_versions").fetchone()["n"] == 0


def test_the_statements_of_the_build_s_own_script_are_not_another_write(catalog):
    # Dataform runs an action as a BigQuery script: the action's job is the script, and the CREATE TABLE that writes
    # the target is a child job of it, as on a live run.
    statement = copy.deepcopy(load("bigquery_jobs.json")[0])
    statement["jobReference"]["jobId"] = "script_job_9f_3"
    statement["statistics"] |= {"creationTime": "1788314390000", "startTime": "1788314390000", "endTime": "1788314391000", "parentJobId": "job-042"}
    sync_history(catalog, extra=[statement])
    dataform.import_invocation(catalog, INVOCATION, client=FakeDataform())
    assert builds.record_handoff(catalog, INVOCATION, TARGET, snapshot_facts())["change"] == "added"
    assert builds.explain_dataset(catalog, SNAPSHOT)["checks"]["written_again_before_snapshot"] is False


def test_a_build_compiled_from_a_workspace_says_it_has_no_commit(catalog):
    compilation = load("dataform_compilation_result.json")
    del compilation["compilationResult"]["gitCommitish"], compilation["compilationResult"]["resolvedGitCommitSha"]
    compilation["compilationResult"]["workspace"] = "projects/workforce-demo/locations/us-central1/repositories/staffing/workspaces/ana"
    dataform.import_invocation(catalog, INVOCATION, client=FakeDataform(compilation=compilation))
    builds.record_handoff(catalog, INVOCATION, TARGET, snapshot_facts())
    trace = builds.explain_dataset(catalog, SNAPSHOT)
    assert trace["defined_in"]["commit"] is None
    assert any("no git commit" in m for m in trace["missing"])


def test_sql_that_cannot_be_traced_gives_a_partial_trace(catalog):
    compilation = load("dataform_compilation_result.json")
    relation = compilation["compilationResultActions"][1]["relation"]
    relation["selectQuery"] = "SELECT worked_minutes_asof / 60 AS hours_at_cutoff, COUNT(*) OVER () AS rows_in_build FROM `workforce-demo.raw.shift_totals`"
    dataform.import_invocation(catalog, INVOCATION, client=FakeDataform(compilation=compilation))
    builds.record_handoff(catalog, INVOCATION, TARGET, snapshot_facts(extra_fields=["rows_in_build"]))
    columns = {c["field"]: c for c in builds.explain_dataset(catalog, SNAPSHOT)["columns"]}
    assert columns["hours_at_cutoff"]["lineage"] == "resolved" and columns["rows_in_build"]["lineage"] == "partial"
    assert "rows_in_build" in " ".join(builds.explain_dataset(catalog, SNAPSHOT)["missing"])

    relation["selectQuery"] = "SELEC hours FROM"  # SQL nothing can read: the definition is recorded, its columns are not known
    dataform.import_invocation(catalog, INVOCATION.replace("build-042", "build-043"), client=FakeDataform(
        invocation=json.loads(json.dumps(load("dataform_workflow_invocation.json")).replace("build-042", "build-043")), compilation=compilation))
    assert catalog.get(f"{TARGET}.hours_at_cutoff")["tags"] == ["lineage:partial"]


def test_an_older_build_imported_late_does_not_turn_back_the_definitions(catalog):
    newer = load("dataform_compilation_result.json")
    newer["compilationResult"] |= {"name": newer["compilationResult"]["name"] + "-next", "createTime": "2026-09-09T00:00:00Z"}
    newer["compilationResultActions"][1]["relation"]["selectQuery"] = (
        "SELECT employee_id, pay_period_start, worked_minutes_asof / 60.0 AS hours_at_cutoff FROM `workforce-demo.raw.shift_totals`")
    later = json.loads(json.dumps(load("dataform_workflow_invocation.json")).replace("build-042", "build-049"))
    later["workflowInvocation"]["resolvedCompilationResult"] = newer["compilationResult"]["name"]
    dataform.import_invocation(catalog, INVOCATION.replace("build-042", "build-049"), client=FakeDataform(invocation=later, compilation=newer))

    late = dataform.import_invocation(catalog, INVOCATION, client=FakeDataform())
    assert late["definitions"] == "kept newer" and late["entries"] == {"added": 0, "changed": 0, "unchanged": 0}
    assert catalog.get(f"{TARGET}.hours_at_cutoff")["definition"] == "worked_minutes_asof / 60.0"
    builds.record_handoff(catalog, INVOCATION, TARGET, snapshot_facts())
    assert "definition" in " ".join(builds.explain_dataset(catalog, SNAPSHOT)["missing"])  # said, not guessed


def test_the_real_client_pages_through_every_action():
    pages = [{"compilationResultActions": [{"n": 1}], "nextPageToken": "t2"}, {"compilationResultActions": [{"n": 2}]}]
    session = Mock()
    session.get.side_effect = [Mock(ok=True, json=Mock(return_value=page)) for page in pages]
    client = dataform.Client(session)
    assert client.query("projects/p/locations/l/repositories/r/compilationResults/c", "compilationResultActions") == [{"n": 1}, {"n": 2}]
    assert session.get.call_args_list[1].kwargs["params"]["pageToken"] == "t2"


def test_the_command_line_imports_a_build_records_its_handoff_and_explains_it(database, monkeypatch, capsys):
    pytest.importorskip("sounding", reason="reading the snapshot goes through Sounding, which needs Python 3.14")
    from odcl import bigquery as bq
    from odcl.cli import main
    monkeypatch.setattr(dataform, "client", lambda: FakeDataform())
    monkeypatch.setattr(bq, "client", lambda project=None: Mock(close=Mock()))
    monkeypatch.setattr(bq, "snapshot", lambda fqn, client: snapshot_facts())
    db = ["--database-url", database]
    assert main([*db, "import", "--dataform", INVOCATION]) == 0
    assert "8 added" in capsys.readouterr().out
    assert main([*db, "handoff", "--invocation", INVOCATION, "--target", TARGET, "--snapshot", SNAPSHOT]) == 0
    assert EXPECTED["read"]["dataset_version"] in capsys.readouterr().out
    assert main([*db, "explain", "dataset", SNAPSHOT]) == 0
    out = capsys.readouterr().out
    assert "worked_minutes_asof / 60" in out and "definitions/shift_inputs.sqlx" in out and "job-042" in out
    assert main([*db, "--json", "explain", "dataset", SNAPSHOT]) == 0
    assert json.loads(capsys.readouterr().out)["reference"] == EXPECTED["read"]["dataset_version"]
    assert main([*db, "explain", "dataset", "workforce-demo.frozen.nothing"]) == 1 and "No dataset version" in capsys.readouterr().err


def test_a_build_whose_sql_counts_through_ctes_registers_no_table_named_after_a_cte(catalog):
    compilation = load("dataform_compilation_result.json")
    compilation["compilationResultActions"][1]["relation"]["selectQuery"] = (
        "WITH base AS (SELECT employee_id, pay_period_start, worked_minutes_asof FROM `workforce-demo.raw.shift_totals`), "
        "with_weekend AS (SELECT *, TRUE AS weekend FROM base), "
        "stats AS (SELECT employee_id, COUNT(*) AS org_shifts FROM with_weekend WHERE worked_minutes_asof > 0 GROUP BY employee_id) "
        "SELECT employee_id, org_shifts FROM stats")
    dataform.import_invocation(catalog, INVOCATION, client=FakeDataform(compilation=compilation))
    assert {t["key"] for t in catalog.find(kind="table")} == {f"table:{RAW}", f"table:{TARGET}"}
    shifts = catalog.get(f"{TARGET}.org_shifts")
    assert [d["key"] for d in shifts["derived_from"]] == [f"column:{RAW}.employee_id", f"column:{RAW}.worked_minutes_asof"]
    assert "lineage:partial" in shifts["tags"]
