# Changelog

Versions follow [semantic versioning](https://semver.org). While the version is `0.x`,
commands, the Python interface and the HTTP API may still change between minor versions,
and the entry says so when one does. The catalog's schema is migrated in place by
`odcl migrate`. The README's [Versioning and releases](README.md#versioning-and-releases)
section says how a release is cut.

## Unreleased

- `odcl diff` (`GET /v1/entries/{key}/diff`, the `what_changed` MCP tool, `Catalog.diff`): what
  changed in a table, with its columns, or a column between two revisions or times, each change
  labelled `cosmetic`, `additive`, `semantic` or `breaking`. What a project uses carries the
  worst change since the use (`severity`), and a used table now counts as changed when one of
  its columns changed. Entry pages show what changed.
- **Users, roles and sign-in.** Users with one role each (`reader`, `editor`, `importer`,
  `admin`) and API tokens (`odcl users ...`; run `odcl migrate` for migration 0011). With
  `$ODCL_TOKEN`, the CLI and `odcl mcp` sign in as that user, record changes under their
  name and check their role; a direct connection without one is trusted as before.
  `Catalog(token=...)` does the same from Python.
- `odcl serve` takes annotations from signed-in editors (`POST /v1/projects`,
  `/v1/projects/{name}/uses`, `/v1/privacy`, `/v1/entries/{key}/created-by`, `/references`,
  `/instances`, `/v1/models/{name}/versions/{version}/relied-on`), manages users and tokens
  (`/v1/me`, `/v1/me/tokens`, `/v1/users`), and gets an Account page. `--require-sign-in`
  closes reads too. OpenLineage events also take an importer's token.
- **Single sign-on** for `odcl serve` through any OpenID Connect provider
  (`ODCL_OIDC_*`, the `sso` extra): authorization code with PKCE, first sign-in makes the
  user with a role from a claim or the admin emails, and tokens of people who stop
  signing in stop working.
- `odcl mcp --server URL` serves the tools through an `odcl serve` with `$ODCL_TOKEN`, so an
  assistant needs no database credentials. The write tools are offered only to an editor or
  above.
- Docs: [Working with several people](docs/working-with-several-people.md): importers run
  centrally under a service user, and people annotate as themselves.
- Imports retire what is gone from their source. A Postgres, SQL-definitions, Dataform, dbt or
  BigQuery import withdraws what it registered before, within what it read, and no longer saw;
  an entry nothing else still says is retired: kept with its history, left out of `find`,
  `tables` and the counts (`--retired` includes it), and listed in `overview` as
  `retired_but_used` when a project uses it. It comes back when its source has it again.
  Import results now always carry `retired` and `withdrawn` lists.
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
