-- Model versions and the fields they were trained on. A build is one model version: the training task that
-- produced it, its immutable artifact, the dataset version it read, and which fields it used in which role.

CREATE TABLE model_builds (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  project_key         text COLLATE "C" NOT NULL REFERENCES entries (key),
  version             text NOT NULL,
  producing_execution bigint NOT NULL REFERENCES executions (id),
  dataset_version_id  bigint NOT NULL REFERENCES dataset_versions (id),
  artifact_uri        text NOT NULL,
  artifact_generation text,
  artifact_sha256     text NOT NULL,
  training_commit     text,
  prediction_cutoff   timestamptz,
  manifest            jsonb NOT NULL,
  manifest_sha256     text NOT NULL,
  recorded_by         text NOT NULL,
  recorded_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_key, version)
);

CREATE INDEX model_builds_dataset_version ON model_builds (dataset_version_id);

-- A field as the training code named it, the role it played, and the dataset version column it came from. A field
-- can be a feature for one model and a label for another.
CREATE TABLE build_inputs (
  build_id            bigint NOT NULL REFERENCES model_builds (id) ON DELETE CASCADE,
  role                text NOT NULL CHECK (role IN ('feature', 'label', 'cohort_filter', 'join_key', 'weight')),
  field               text NOT NULL,
  position            integer,
  dataset_version_id  bigint NOT NULL,
  field_path          text NOT NULL,
  PRIMARY KEY (build_id, role, field),
  FOREIGN KEY (dataset_version_id, field_path) REFERENCES version_columns (dataset_version_id, field_path)
);

CREATE INDEX build_inputs_column ON build_inputs (dataset_version_id, field_path);
