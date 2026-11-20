-- A second kind of dataset version: a set of Cloud Storage files a training run read, such as the Parquet a pipeline's
-- data-loader task exported. Existing rows are BigQuery snapshots and keep every field they have.

ALTER TABLE dataset_versions
  ADD COLUMN kind text NOT NULL DEFAULT 'bigquery_snapshot' CHECK (kind IN ('bigquery_snapshot', 'gcs_files')),
  -- for a file set: the artifact URI or prefix the files sit under
  ADD COLUMN source_uri text COLLATE "C",
  ALTER COLUMN snapshot_table DROP NOT NULL,
  ALTER COLUMN snapshot_time DROP NOT NULL,
  -- for a file set: the catalog table the files were exported from, when known (table-level lineage only)
  ALTER COLUMN table_key DROP NOT NULL,
  -- for a file set: the task that wrote the files, when known. A producer is never guessed.
  ALTER COLUMN producer_execution DROP NOT NULL,
  ADD CONSTRAINT dataset_versions_snapshot_complete CHECK (
    kind <> 'bigquery_snapshot' OR (snapshot_table IS NOT NULL AND snapshot_time IS NOT NULL AND table_key IS NOT NULL AND producer_execution IS NOT NULL)),
  ADD CONSTRAINT dataset_versions_files_located CHECK (kind <> 'gcs_files' OR source_uri IS NOT NULL);

-- The objects of a file set, as Cloud Storage reported them when it was recorded. The generation pins the exact bytes.
CREATE TABLE dataset_version_objects (
  dataset_version_id  bigint NOT NULL REFERENCES dataset_versions (id) ON DELETE CASCADE,
  uri                 text COLLATE "C" NOT NULL,
  generation          text NOT NULL,
  size                bigint,
  crc32c              text,
  md5                 text,
  PRIMARY KEY (dataset_version_id, uri)
);

-- A file set's fields come from its Parquet schema, not from a catalog column, so they pin no column revision and
-- claim no lineage.
ALTER TABLE version_columns
  ALTER COLUMN column_key DROP NOT NULL,
  ALTER COLUMN column_revision DROP NOT NULL,
  ADD COLUMN dtype text,
  DROP CONSTRAINT version_columns_lineage_status_check,
  ADD CONSTRAINT version_columns_lineage_status_check CHECK (lineage_status IN ('resolved', 'partial', 'unresolved', 'not_claimed')),
  ADD CONSTRAINT version_columns_pinned_together CHECK ((column_key IS NULL) = (column_revision IS NULL));
