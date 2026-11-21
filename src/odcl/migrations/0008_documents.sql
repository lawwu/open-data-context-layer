-- Documents: Markdown pages that describe tables, columns and projects. A `describes` link says which part of the page
-- (a heading anchor) describes the target and, optionally, what it says about it in one line.

ALTER TABLE revision_links
  ADD COLUMN section text,
  ADD COLUMN says text;

DROP VIEW current_links;
CREATE VIEW current_links AS
SELECT s.key AS source_key, l.relation, l.target_key, l.target_revision, l.purpose, t.head_revision AS target_head, l.section, l.says
FROM entries s
JOIN revision_links l ON l.source_revision = s.head_revision
JOIN entries t ON t.key = l.target_key;
