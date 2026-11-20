-- Runs and the data they produced. An execution is one run of something: a Dataform invocation, one action in
-- it, and later a Vertex pipeline task. A dataset version is one immutable copy of a table, bound to the run
-- that produced it, with the definition of each of its columns pinned as that run used it.

CREATE TABLE executions (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  system              text NOT NULL,
  native_key          text NOT NULL,
  kind                text NOT NULL,
  parent_id           bigint REFERENCES executions (id),
  -- the revision of the definition this run used, when the catalog holds it
  definition_revision text REFERENCES revisions (id),
  state               text NOT NULL,
  started_at          timestamptz,
  ended_at            timestamptz,
  -- the BigQuery job that did the work. No foreign key: job history may be synced before or after the run.
  job_project         text,
  job_location        text,
  job_id              text,
  evidence            jsonb NOT NULL DEFAULT '{}',
  observed_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (system, native_key)
);

CREATE INDEX executions_parent ON executions (parent_id) WHERE parent_id IS NOT NULL;
CREATE INDEX executions_job ON executions (job_project, job_id) WHERE job_id IS NOT NULL;

CREATE TABLE dataset_versions (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  -- project.dataset.table@time of the immutable copy, e.g. a table snapshot
  reference           text COLLATE "C" NOT NULL UNIQUE,
  snapshot_table      text COLLATE "C" NOT NULL,
  snapshot_time       timestamptz NOT NULL,
  -- the logical table it copies, and that table's definition as the producing run used it
  table_key           text COLLATE "C" NOT NULL,
  table_revision      text,
  producer_execution  bigint NOT NULL REFERENCES executions (id),
  verification        jsonb NOT NULL,
  recorded_by         text NOT NULL,
  recorded_at         timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (table_key, table_revision) REFERENCES revisions (entry_key, id)
);

CREATE INDEX dataset_versions_snapshot ON dataset_versions (lower(snapshot_table), snapshot_time);
CREATE INDEX dataset_versions_producer ON dataset_versions (producer_execution);

-- Each field of a dataset version and the column revision that defined it. The column revision's own links pin
-- the upstream columns it was computed from.
CREATE TABLE version_columns (
  dataset_version_id  bigint NOT NULL REFERENCES dataset_versions (id) ON DELETE CASCADE,
  field_path          text NOT NULL,
  column_key          text COLLATE "C" NOT NULL,
  column_revision     text NOT NULL,
  lineage_status      text NOT NULL CHECK (lineage_status IN ('resolved', 'partial', 'unresolved')),
  PRIMARY KEY (dataset_version_id, field_path),
  FOREIGN KEY (column_key, column_revision) REFERENCES revisions (entry_key, id)
);

CREATE INDEX version_columns_column ON version_columns (column_key);
