-- Entries, their immutable revisions, and the links each revision pins.
-- Keys sort bytewise ("C") so listings come back in the same order on every server.

CREATE TABLE entries (
  key           text COLLATE "C" PRIMARY KEY,
  kind          text NOT NULL,
  head_revision text NOT NULL,
  search        tsvector NOT NULL DEFAULT ''::tsvector
);

CREATE TABLE revisions (
  id          text PRIMARY KEY,
  entry_key   text COLLATE "C" NOT NULL REFERENCES entries (key) DEFERRABLE INITIALLY DEFERRED,
  body        jsonb NOT NULL,
  digest      text NOT NULL,
  actor       text NOT NULL,
  recorded_at timestamptz NOT NULL,
  seq         bigint GENERATED ALWAYS AS IDENTITY,
  UNIQUE (entry_key, id)
);

-- An entry's head is one of its own revisions.
ALTER TABLE entries ADD CONSTRAINT entries_head_is_own_revision
  FOREIGN KEY (key, head_revision) REFERENCES revisions (entry_key, id) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX entries_search ON entries USING gin (search);
CREATE INDEX entries_kind ON entries (kind);
CREATE INDEX revisions_history ON revisions (entry_key, recorded_at, seq);
CREATE INDEX revisions_table ON revisions ((body ->> 'table')) WHERE body ->> 'kind' = 'column';

-- What one revision pointed at, and the revision of the target it saw. The pinned revision belongs to the target.
CREATE TABLE revision_links (
  source_revision text NOT NULL REFERENCES revisions (id),
  relation        text NOT NULL,
  target_key      text COLLATE "C" NOT NULL,
  target_revision text NOT NULL,
  purpose         text,
  PRIMARY KEY (source_revision, relation, target_key),
  FOREIGN KEY (target_key, target_revision) REFERENCES revisions (entry_key, id)
);

CREATE INDEX revision_links_target ON revision_links (target_key, relation);

-- The links of every entry's head revision, with the head its target has now.
CREATE VIEW current_links AS
SELECT s.key AS source_key, l.relation, l.target_key, l.target_revision, l.purpose, t.head_revision AS target_head
FROM entries s
JOIN revision_links l ON l.source_revision = s.head_revision
JOIN entries t ON t.key = l.target_key;
