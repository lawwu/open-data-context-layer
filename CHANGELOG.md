# Changelog

Versions follow [semantic versioning](https://semver.org). While the version is `0.x`,
commands, the Python interface and the HTTP API may still change between minor versions,
and the entry says so when one does. The catalog's schema is migrated in place by
`odcl migrate`. The README's [Versioning and releases](README.md#versioning-and-releases)
section says how a release is cut.

## Unreleased

## 0.2.0 (2026-11-22)

The first release. odcl was installed from `main` before this, already reporting 0.2.0; what
follows is what changed since then. Run `odcl migrate` after upgrading (migrations up to 0014).

- The web page's Account page has an Alerts panel: an editor lists, adds and removes their alert
  subscriptions; an admin sees everyone's.
- The command line works through an `odcl serve` with `--server URL` or `$ODCL_SERVER`, signed in
  with `$ODCL_TOKEN`, so people read and annotate without database credentials. Commands that
  read from where the data is (imports, syncs) still need a database connection, and say so.
- **Security fixes** from a review of sign-in and the HTTP API:
  - Single sign-on counts an email only when the provider says it is verified (`email_verified`
    true, or Entra's `xms_edov`), for `ODCL_OIDC_ADMIN_EMAILS` and for taking over a user an admin
    made. A missing claim, or the string `"false"`, made an admin before.
  - Someone who did not sign in is no longer taken for a trusted direct connection: they could
    revoke anyone's token and read every alert subscription. Every HTTP write needs a signed-in
    user, and revoking says nothing of whose a token is.
  - Webhooks: a redirect is a failed delivery, an internal address is refused unless
    `$ODCL_ALERT_ALLOW_HOSTS` allows it, errors are generic, and listing shows only the host.
  - OpenLineage events without a token are refused once the first user is made.
  - `odcl mcp --server` and the CLI do not follow a server's redirects, so a token is not sent on.
  - The sign-in cookie is signed (`$ODCL_SESSION_SECRET`) and where it returns to is checked again;
    single sign-on on a public address needs an `https://` `ODCL_OIDC_REDIRECT_URI`.
  - Behind a proxy, a browser's change is checked against `$ODCL_PUBLIC_URL`, not the `Host` a
    proxy rewrites. A request the server fails on gets a 500 instead of a dropped connection, and
    a client has 30 seconds to send one.
- **One read of what projects are exposed to** (`odcl impact`, `GET /v1/impact`,
  `odcl.impact`): data a project uses that changed, is gone, or is failing a test. The overview,
  project pages and alerts all read it, so alerts now also tell of failing data-quality tests
  (run `odcl migrate` for migration 0014), and the project list counts a used table whose
  column changed, as the overview does.
- **One result shape for every import** of tables and columns: `added`, `changed`, `unchanged`,
  `retired`, `withdrawn`, `unresolved` and `skipped`, always present. This changes three:
  BigQuery's `unresolved_references` is now `unresolved`; a Dataform import's counts are at the
  top level (were under `entries`); a PostgreSQL import names keys to schemas not read, and tables
  a view reads there, in `unresolved` (were in `skipped`). Importers write through
  `Catalog.importing(importer, scope=…)` (`odcl.imports`).
- **One operation table** behind the command line, the HTTP API and the MCP tools
  (`odcl.operations`), so every command, route and tool is declared once. New commands for what
  only the API offered: `odcl tables`, `odcl projects`, `odcl models`, `odcl runs` and
  `odcl graph KEY`. Results now have one shape everywhere, which changes a few:
  `odcl privacy set --json` answers `{"changed": [...]}` (was `entries`), `odcl privacy export
  --json` and `odcl docs --json` answer a list (as their HTTP routes did),
  `GET /v1/privacy/proposals` answers `{"proposals": [...]}` and `GET /v1/popular`
  `{"since", "tables"}` (as the command line did), `odcl popular` lists 50 by default (was 20),
  and `GET /v1/projects/{name}/uses` takes `revision` like the command line.
- **Lineage explorer** on the web page (`#/lineage/KEY`): every edge around a table or column,
  by direction and steps, as columns or tables, with the projects that use them; hover to
  follow a path, click for details. `GET /v1/entries/{key}/graph` and `Catalog.graph` return
  the nodes and edges.
- **Popularity** from the synced job history: `odcl popular`, `GET /v1/popular`, a `reads`
  count (last 30 days) on every table with `sort=reads`, a table's `usage` with the tables most
  often read with it, and search lifting matches on much-read tables a little.
- `odcl privacy propose` suggests `pii` and a data category for columns that say nothing yet,
  from their names, glossary terms and descriptions, with a reason and a confidence;
  `--write` puts them in a file to review and `odcl privacy apply` records the accepted ones.
  `GET /v1/privacy/proposals`; table pages list such columns.
- **Glossary terms and domains**: two new kinds of entry. Columns and tables `means` terms;
  tables, projects and terms are `in_domain` a domain (`odcl term add`, `odcl domain add`,
  `odcl means`, `odcl in-domain`, `odcl glossary`, `/v1/glossary`, `/v1/terms`, `/v1/domains`).
  Imports keep them; search finds terms by synonym; documents can describe them; the web page
  has a Glossary. `overview` counts now include `term` and `domain`.
- **Data-quality results** (`odcl import --quality FILE.jsonl` or `--dbt-tests TARGET`,
  `odcl quality KEY [--at]`, `/v1/entries/{key}/quality`, `POST /v1/quality`): test outcomes
  kept once each and pinned to the revision current when the test ran. Entries show their
  newest results, `overview` lists `failing_and_used`, and a model version's trace lists the
  tests failing on its inputs when it was trained. Run `odcl migrate` for migration 0013.
- **Alerts** (`odcl alerts subscribe|list|remove|send`, `/v1/alerts/…`): a project's people are
  told by webhook, Slack or email when data the project uses changes after it used it, or is
  gone from its source, at or above a severity. Each change is sent once per subscription;
  failed deliveries are retried. Run `odcl migrate` for migration 0012.
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
