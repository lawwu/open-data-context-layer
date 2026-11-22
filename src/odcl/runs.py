"""The life of a run, in one place.

Every system that reports runs names their states its own way: Dataform says SUCCEEDED, OpenLineage COMPLETE,
Vertex AI PIPELINE_STATE_SUCCEEDED, BigQuery DONE (and DONE with an error is a failure). The catalog keeps each
system's own word as `state`, and reads it as one `outcome` from this table:

    running ──► succeeded | failed | cancelled | skipped
    unknown     a state this table does not name: never read as finished

A finished run keeps its outcome. A later, unfinished reading of it (an event delivered late, an older poll)
never moves it back to running; `hold_finished` is that rule as SQL for an upsert into `executions`.
"""
from __future__ import annotations

RUNNING, SUCCEEDED, FAILED, CANCELLED, SKIPPED, UNKNOWN = "running", "succeeded", "failed", "cancelled", "skipped", "unknown"
OUTCOMES = (RUNNING, SUCCEEDED, FAILED, CANCELLED, SKIPPED, UNKNOWN)
FINISHED = (SUCCEEDED, FAILED, CANCELLED, SKIPPED)

# each system's state names and the outcome each means
STATES: dict[str, dict[str, str]] = {
    # workflow invocations and their actions
    "dataform": {"PENDING": RUNNING, "RUNNING": RUNNING, "CANCELING": RUNNING, "SUCCEEDED": SUCCEEDED, "FAILED": FAILED,
                 "CANCELLED": CANCELLED, "SKIPPED": SKIPPED, "DISABLED": SKIPPED},
    # pipeline jobs (PIPELINE_STATE_*) and their tasks
    "vertex": {"PIPELINE_STATE_QUEUED": RUNNING, "PIPELINE_STATE_PENDING": RUNNING, "PIPELINE_STATE_RUNNING": RUNNING,
               "PIPELINE_STATE_CANCELLING": RUNNING, "PIPELINE_STATE_PAUSED": RUNNING, "PIPELINE_STATE_SUCCEEDED": SUCCEEDED,
               "PIPELINE_STATE_FAILED": FAILED, "PIPELINE_STATE_CANCELLED": CANCELLED,
               "PENDING": RUNNING, "RUNNING": RUNNING, "CANCEL_PENDING": RUNNING, "CANCELLING": RUNNING, "SUCCEEDED": SUCCEEDED,
               "FAILED": FAILED, "CANCELLED": CANCELLED, "SKIPPED": SKIPPED, "NOT_TRIGGERED": SKIPPED},
    # run event types; OTHER says nothing about where the run is
    "openlineage": {"START": RUNNING, "RUNNING": RUNNING, "COMPLETE": SUCCEEDED, "FAIL": FAILED, "ABORT": CANCELLED, "OTHER": UNKNOWN},
    # query jobs: DONE is finished either way, and an error result makes it a failure
    "bigquery": {"PENDING": RUNNING, "RUNNING": RUNNING, "DONE": SUCCEEDED},
}


def outcome(system: str, state: str | None, error: object = None) -> str:
    """What a run in this state came to. `error` is BigQuery's error reason or result: a finished job with one failed."""
    found = STATES.get(system, {}).get(state or "", UNKNOWN)
    return FAILED if found == SUCCEEDED and error else found


def finished(system: str, state: str | None) -> bool:
    return outcome(system, state) in FINISHED


def succeeded(system: str, state: str | None, error: object = None) -> bool:
    return outcome(system, state, error) == SUCCEEDED


def finished_states(system: str) -> tuple[str, ...]:
    return tuple(sorted(s for s, o in STATES[system].items() if o in FINISHED))


def finished_sql(system: str) -> str:
    """The finished states as a SQL list, such as ('DONE'), for `state IN` / `state NOT IN`."""
    return "(" + ", ".join(f"'{s}'" for s in finished_states(system)) + ")"  # constants from STATES, never input


def hold_finished(system: str) -> str:
    """The SET expression for `executions.state` in an upsert: a run already finished keeps its state."""
    return f"CASE WHEN executions.state IN {finished_sql(system)} THEN executions.state ELSE excluded.state END"
