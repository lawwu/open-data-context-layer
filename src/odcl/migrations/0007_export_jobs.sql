-- A file set bound to the BigQuery EXPORT DATA job that wrote it. The job's SELECT is the definition of each file
-- field, so a bound field is traced to the source columns it is computed from, pinned to a catalog revision.

-- when each object was created and how many rows its Parquet footer reports, to check the files are the job's output
ALTER TABLE dataset_version_objects
  ADD COLUMN time_created timestamptz,
  ADD COLUMN num_rows bigint;

-- a bound field's expression in the job's SELECT, as written
ALTER TABLE version_columns ADD COLUMN expression text;

-- the export job(s) that wrote a file set, as BigQuery reported them. The SQL is kept as its digest, and as its shape
-- (literals replaced) unless the binding was recorded with --sql none.
CREATE TABLE dataset_version_exports (
  dataset_version_id  bigint NOT NULL REFERENCES dataset_versions (id) ON DELETE CASCADE,
  job_project         text NOT NULL,
  job_location        text NOT NULL,
  job_id              text NOT NULL,
  created_at          timestamptz NOT NULL,
  ended_at            timestamptz,
  output_uri          text NOT NULL,
  file_count          bigint,
  row_count           bigint,
  sql_sha256          text NOT NULL,
  sql_template        text,
  row_filter          text,
  recorded_by         text NOT NULL,
  recorded_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (dataset_version_id, job_project, job_location, job_id)
);

-- the catalog columns a bound field is computed from, at the revision pinned: one recorded before the export job ran
-- when the catalog has one, otherwise the revision current when the binding was recorded
CREATE TABLE version_column_sources (
  dataset_version_id  bigint NOT NULL,
  field_path          text NOT NULL,
  source_key          text COLLATE "C" NOT NULL,
  source_revision     text NOT NULL,
  pinned              text NOT NULL CHECK (pinned IN ('before_export', 'at_binding')),
  PRIMARY KEY (dataset_version_id, field_path, source_key),
  FOREIGN KEY (dataset_version_id, field_path) REFERENCES version_columns (dataset_version_id, field_path) ON DELETE CASCADE,
  FOREIGN KEY (source_key, source_revision) REFERENCES revisions (entry_key, id)
);

-- the columns the export job's WHERE filters rows on: which rows the files hold, not where a value comes from
CREATE TABLE version_row_filters (
  dataset_version_id  bigint NOT NULL REFERENCES dataset_versions (id) ON DELETE CASCADE,
  source_key          text COLLATE "C" NOT NULL,
  source_revision     text NOT NULL,
  pinned              text NOT NULL CHECK (pinned IN ('before_export', 'at_binding')),
  PRIMARY KEY (dataset_version_id, source_key),
  FOREIGN KEY (source_key, source_revision) REFERENCES revisions (entry_key, id)
);
