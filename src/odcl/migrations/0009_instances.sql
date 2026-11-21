-- Copies of one definition: a definition table's `instance_of` links point at the tables that are copies of it (the
-- same table in each tenant's project, say), resolved from glob patterns the definition keeps. Lineage reads a copy
-- as built from its definition, and each column of a copy as built from the definition's column of the same name.

CREATE INDEX revision_links_instances ON revision_links (source_revision) WHERE relation = 'instance_of';

-- The definitions, found whenever a table is registered, to link it if it is a copy of one.
CREATE INDEX revisions_instances ON revisions (entry_key) WHERE body ? 'instances';

-- current_links finds an entry by its head revision; so do the copy edges below.
CREATE UNIQUE INDEX entries_head ON entries (head_revision);

-- The lineage edges copies add: a copy is built from its definition, and each of its columns from the definition's
-- column of the same name (source_key is built from target_key, as for derived_from). They pin nothing, so they are
-- never "changed since". A walk reads these once and derived_from links by index at each step.
CREATE VIEW copy_edges (source_key, target_key, target_revision, target_head) AS
SELECT l.target_key, l.source_key, d.head_revision, d.head_revision
FROM current_links l JOIN entries d ON d.key = l.source_key
WHERE l.relation = 'instance_of'
UNION ALL
-- (keys sort as "C"; revisions_table indexes the column's table in the default collation, so the key is compared in that)
SELECT cc.key, dc.key, dc.head_revision, dc.head_revision
FROM current_links l
JOIN revisions ccr ON ccr.body ->> 'kind' = 'column' AND ccr.body ->> 'table' = l.target_key COLLATE "default"
JOIN entries cc ON cc.head_revision = ccr.id
JOIN revisions dcr ON dcr.body ->> 'kind' = 'column' AND dcr.body ->> 'table' = l.source_key COLLATE "default" AND dcr.body ->> 'name' = ccr.body ->> 'name'
JOIN entries dc ON dc.head_revision = dcr.id
WHERE l.relation = 'instance_of';
