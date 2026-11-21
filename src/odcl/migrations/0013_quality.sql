-- Data-quality results: a test (dbt, an assertion, a Great Expectations check…) run against a table or column, with
-- its outcome, pinned to the revision of the entry that was current when it ran. So "was this column failing when
-- the model trained on it?" has an answer. A result is kept once per source, run, test and entry.

CREATE TABLE quality_results (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entry_key      text COLLATE "C" NOT NULL REFERENCES entries (key),
  entry_revision text NOT NULL,
  source         text NOT NULL,
  run            text NOT NULL,
  test           text NOT NULL,
  test_kind      text,
  status         text NOT NULL CHECK (status IN ('pass', 'warn', 'fail', 'error', 'skip')),
  failures       bigint,
  message        text,
  observed_at    timestamptz NOT NULL,
  recorded_by    text NOT NULL,
  recorded_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, run, test, entry_key),
  FOREIGN KEY (entry_key, entry_revision) REFERENCES revisions (entry_key, id)
);

CREATE INDEX quality_results_entry ON quality_results (entry_key, observed_at DESC);
