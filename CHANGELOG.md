# Changelog

Versions follow [semantic versioning](https://semver.org). While the version is `0.x`,
commands, the Python interface and the HTTP API may still change between minor versions,
and the entry says so when one does. The catalog's schema is migrated in place by
`odcl migrate`. The README's [Versioning and releases](README.md#versioning-and-releases)
section says how a release is cut.

## Unreleased

- **Breaking: Python 3.14 or later is required**, as Sounding, which the BigQuery commands
  read through, requires it.
- The tables page and column lists answer in a few queries instead of several per row
  (1,000 tables: about 95 s to 0.25 s), with the same output.
- Lineage lower-cases every name in the SQL it traces, as keys are, so `FROM Proj.DS.Orders`
  and a quoted `"CreatedAt"` match their keys, including through `SELECT *`.
- Imports take every entry lock first: an import of any size fits PostgreSQL's lock table,
  and two imports run one after the other instead of deadlocking.
- New imports: `--postgres` (tables, views, comments, view lineage and foreign keys) and
  `--sql-definitions FILE.jsonl` (SQL definitions from any source). SQL imports name the
  tables they could not resolve and the SQL they could not trace.
- New links and fields: `instance_of` from a definition table to its copies, by path pattern
  (`odcl instances`), with used-by and lineage going through copies; `references` for
  foreign keys; `created_by` for what made a table or file; `pii`, `scrubbing`,
  `data_category` and `retention` (`odcl privacy set|export`); `relied_on` documents pinned
  by a model version (`odcl rely`). Run `odcl migrate` (migrations 0009 and 0010).
- `sync bigquery-jobs --since/--until` or `--pipeline-run JOB [--task T]` reads one window.
- `odcl discover vertex-runs` and `odcl propose vertex-manifest` draft a training manifest.
- Under merge, each importer replaces only its own tags. A document's odcl block can be an
  HTML comment.
- **Breaking: the Fathom name is gone.** The `fathom` command (use `odcl`), the `fathom_data`
  package (use `odcl`; `Fathom` was `odcl.Catalog`) and `$FATHOM_USER` (use `$ODCL_USER`)
  no longer exist. `odcl import-sqlite`, which brought an old Fathom SQLite catalog across,
  is removed too: migrate such a file with an earlier version first.
- Releases: a `vX.Y.Z` tag on `main` builds the sdist and wheel, attests them and publishes
  them as a GitHub release with this changelog's notes; a `latest` tag follows the newest
  release.
- Docs: a site on GitHub Pages, https://lawwu.github.io/open-data-context-layer/, built with
  Zensical from the README, this changelog and `docs/`, and published on every push to `main`.
- Workflows: every action is locked to a commit in `.github/workflows/actions.lock`, checked
  on every pull request with zizmor.
