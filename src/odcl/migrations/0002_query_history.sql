-- BigQuery query-job history: which jobs ran, what each reported reading and writing, and how far each sync got.
-- A job is known by where it ran, not by the tables it touched: (execution project, location, job id).

-- One reusable query shape per execution project. Literals are already replaced; original SQL is never stored here.
CREATE TABLE query_patterns (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  scope           text NOT NULL,
  template_digest text NOT NULL,
  template        text NOT NULL,
  first_seen_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scope, template_digest)
);

CREATE TABLE query_jobs (
  execution_project text NOT NULL,
  location          text NOT NULL,
  job_id            text NOT NULL,
  parent_job_id     text,
  created_at        timestamptz NOT NULL,
  started_at        timestamptz,
  ended_at          timestamptz,
  state             text NOT NULL,
  statement_type    text,
  error_reason      text,
  cache_hit         boolean,
  processed_bytes   bigint,
  billed_bytes      bigint,
  slot_ms           bigint,
  pattern_id        bigint REFERENCES query_patterns (id),
  -- what could not be learned about this job, e.g. no_table_references or template_unavailable
  gaps              text[] NOT NULL DEFAULT '{}',
  observed_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (execution_project, location, job_id)
);

CREATE INDEX query_jobs_created ON query_jobs (execution_project, created_at);
CREATE INDEX query_jobs_parent ON query_jobs (execution_project, parent_job_id) WHERE parent_job_id IS NOT NULL;
CREATE INDEX query_jobs_unfinished ON query_jobs (execution_project, created_at) WHERE state <> 'DONE';
CREATE INDEX query_jobs_pattern ON query_jobs (pattern_id) WHERE pattern_id IS NOT NULL;

-- One observation per job, table and role. `read` is a table the job reported referencing; `write` is the
-- destination of a statement that succeeded and named a lasting table. table_key keeps BigQuery's spelling.
CREATE TABLE query_tables (
  execution_project text NOT NULL,
  location          text NOT NULL,
  job_id            text NOT NULL,
  table_key         text COLLATE "C" NOT NULL,
  role              text NOT NULL CHECK (role IN ('read', 'write')),
  evidence          text NOT NULL,
  PRIMARY KEY (execution_project, location, job_id, table_key, role),
  FOREIGN KEY (execution_project, location, job_id) REFERENCES query_jobs ON DELETE CASCADE
);

CREATE INDEX query_tables_table ON query_tables (lower(table_key), role);

-- The end of the last window a source finished reading in full, per execution project and location.
CREATE TABLE sync_cursors (
  source            text NOT NULL,
  execution_project text NOT NULL,
  location          text NOT NULL,
  completed_until   timestamptz NOT NULL,
  updated_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source, execution_project, location)
);
