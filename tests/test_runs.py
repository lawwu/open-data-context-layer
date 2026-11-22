"""One reading of run states across systems: what each state means, and which ones a later reading cannot undo."""
import pytest

from odcl import runs


def test_each_system_holds_exactly_the_states_it_held_before_the_table_was_shared():
    assert runs.finished_states("dataform") == ("CANCELLED", "DISABLED", "FAILED", "SKIPPED", "SUCCEEDED")
    assert runs.finished_states("openlineage") == ("ABORT", "COMPLETE", "FAIL")
    # NOT_TRIGGERED is new: a task a finished pipeline never started will not start later
    assert runs.finished_states("vertex") == ("CANCELLED", "FAILED", "NOT_TRIGGERED", "PIPELINE_STATE_CANCELLED", "PIPELINE_STATE_FAILED",
                                              "PIPELINE_STATE_SUCCEEDED", "SKIPPED", "SUCCEEDED")
    assert runs.finished_states("bigquery") == ("DONE",)


@pytest.mark.parametrize("system, state, error, outcome", [
    ("dataform", "SUCCEEDED", None, "succeeded"), ("dataform", "DISABLED", None, "skipped"), ("dataform", "RUNNING", None, "running"),
    ("vertex", "PIPELINE_STATE_FAILED", None, "failed"), ("vertex", "PIPELINE_STATE_PAUSED", None, "running"),
    ("openlineage", "COMPLETE", None, "succeeded"), ("openlineage", "ABORT", None, "cancelled"), ("openlineage", "OTHER", None, "unknown"),
    ("bigquery", "DONE", None, "succeeded"), ("bigquery", "DONE", "accessDenied", "failed"), ("bigquery", "RUNNING", "x", "running"),
    ("dataform", "UNKNOWN", None, "unknown"), ("dataform", None, None, "unknown"), ("nobody", "SUCCEEDED", None, "unknown"),
])
def test_a_state_reads_as_one_outcome(system, state, error, outcome):
    assert runs.outcome(system, state, error) == outcome
    assert runs.succeeded(system, state, error) == (outcome == "succeeded")


def test_an_unknown_state_is_never_read_as_finished():
    assert not runs.finished("openlineage", "OTHER") and not runs.finished("dataform", "SOMETHING_NEW")


def test_the_upsert_keeps_a_finished_state():
    assert runs.hold_finished("bigquery") == "CASE WHEN executions.state IN ('DONE') THEN executions.state ELSE excluded.state END"
