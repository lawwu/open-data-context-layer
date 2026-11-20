-- OpenLineage events as received, so a replay is recognised and the evidence behind a run stays readable. An event is
-- kept as the catalog stores everything else: SQL only as its shape, and no error messages.

CREATE TABLE lineage_events (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  digest       text NOT NULL UNIQUE,
  run_id       text,
  event_type   text,
  event_time   timestamptz NOT NULL,
  job          text,
  producer     text,
  body         jsonb NOT NULL,
  received_by  text NOT NULL,
  received_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX lineage_events_run ON lineage_events (run_id, event_time) WHERE run_id IS NOT NULL;
