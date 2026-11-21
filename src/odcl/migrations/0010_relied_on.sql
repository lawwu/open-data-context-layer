-- The documents a model version relied on, such as a finding about how a field behaves, each at the revision the
-- model was built on. A later correction of the page is a new revision, so the catalog can say which model versions
-- used the old text.

CREATE TABLE build_documents (
  build_id          bigint NOT NULL REFERENCES model_builds (id) ON DELETE CASCADE,
  document_key      text COLLATE "C" NOT NULL,
  document_revision text NOT NULL,
  pinned_by         text NOT NULL CHECK (pinned_by IN ('revision', 'digest', 'at_training', 'current')),
  why               text,
  recorded_by       text NOT NULL,
  recorded_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (build_id, document_key),
  FOREIGN KEY (document_key, document_revision) REFERENCES revisions (entry_key, id)
);

CREATE INDEX build_documents_document ON build_documents (document_key);
