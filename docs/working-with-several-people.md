# Working with several people

Who should write to the catalog once more than one person uses it: everyone, or one admin? This page lays out the
options for odcl as it is built, the one chosen, and what that means for credentials, the HTTP API, MCP, and hand
edits versus imports. The [README](../README.md#users-and-sign-in) says how to set it up.

## What odcl already gives several writers

- **Every change is attributed and kept.** Each write is a new immutable revision with its `actor` and time;
  `odcl show` lists them. Nothing is updated in place.
- **Imports are all-or-nothing and serialized.** `Catalog.apply` registers a whole batch in one transaction, and
  takes all 512 advisory-lock stripes first, so two imports run one after the other and never deadlock. A single
  write takes only its entry's stripe, so annotations do not wait behind each other.
- **Importers do not erase one another.** Under `merge`, each importer keeps its details under its own name in
  `source` and replaces only its own tags.
- **Hand-set annotations survive imports.** An import that does not merge (dbt, for one) still keeps what was said
  beside it with its own commands: `instances`, `created_by`, a column's `references`, and the privacy fields
  (`pii`, `scrubbing`, `data_category`, `retention`). Table importers never write projects, their `uses`, documents or `relied_on`; a
  model-version import adds its own `uses` beside the ones people recorded and keeps theirs.

What it lacked was any notion of *who may* write: anyone holding the database URL could write anything, under any
name they chose (`--as`, `$ODCL_USER`), and the HTTP server and MCP server could not tell callers apart.

## The options

**1. Everyone writes directly, as themselves.** Each person gets database credentials and runs imports and
annotations from their own machine. The revision history records who did what, and the locks and merge rules
above keep concurrent writers safe.
*For:* nothing to run centrally. *Against:* everyone holds write credentials to the whole database, so the
recorded actor is only what each person claims; imports run from laptops with whatever versions of dbt projects
and credentials happen to be there; a broken import can come from anyone.

**2. Everything through one admin.** One person (or one service identity) runs every import and makes every
edit; everyone else reads.
*For:* simplest to reason about. *Against:* the people who know what a table means, which project used it, or
whether a column is personal data cannot say so themselves; the admin becomes a queue, and annotations stop
being made.

**3. A mix, with roles (chosen).** Importers run centrally, from CI or a scheduler, under a service user. People
annotate as themselves through the HTTP API, the web page or MCP, with a role that says what they may do.

## The design

### Roles

| Role | May |
|---|---|
| `reader` | read everything |
| `editor` | also annotate: register projects and record their `uses`; privacy fields; `created_by`; `references`; copies (`instances`); documents a model version relied on |
| `importer` | also import: whole batches (`Catalog.apply`, every `odcl import`), builds and handoffs, model versions, query history, OpenLineage events |
| `admin` | also manage users and their tokens |

Users live in the catalog's own database (`users`, `user_tokens`, `sessions`; migration 0011). A user's name is
what revisions record as the actor.

### Who holds what

- **Database credentials** are held by the importers' runner (CI or a scheduler) and the few admins who run
  `odcl migrate` and make the first users. Nobody else needs them.
- **A direct database connection without a token is trusted**, as before: its credentials are the gate, and its
  writes are recorded under `--as`, `$ODCL_USER` or the login name. This is how the first admin is made, and how a
  scheduled import is signed (`ODCL_USER=etl`), or it can use a service user's token (`ODCL_TOKEN`) so its role is
  checked too.
- **Everyone else signs in to `odcl serve`**: with single sign-on through the organization's OpenID Connect
  provider in the browser, or with a personal API token (`odcl_...`) for scripts and MCP. Tokens are kept only as
  digests and shown once. A single-sign-on user's tokens stop working after 30 days without signing in, so people
  removed from the provider lose API access too.

### What a write requires

- **HTTP:** every write needs a signed-in user whose role allows it. A browser session counts for a write only
  when the request comes from the server's own page (its `Origin`), so another site cannot write in a signed-in
  browser's name. Reads are open unless the server runs with `--require-sign-in` or single sign-on, which close
  them too. OpenLineage events need an importer's token, or the shared `ODCL_LINEAGE_TOKEN`.
- **MCP:** `odcl mcp --server https://catalog.example.com` with `$ODCL_TOKEN` reads and writes through the HTTP API,
  so a person's assistant needs no database credentials and its writes are that person's. The write tools are
  offered only with `--allow-writes` and only to an editor or above. `odcl mcp` against the database directly
  works as before, and with `$ODCL_TOKEN` it checks that user's role.
- **CLI:** with `$ODCL_TOKEN` set, the CLI signs in as that user and their role is checked; without it, it is a
  trusted direct connection.

### Hand edits and importer writes

The split of roles follows the split of fields, so the two kinds of writer rarely touch the same thing:

- **Importers own what their source says**: an imported table's description, owner, columns, lineage and tags.
  Re-importing replaces it, as it should; change those in the source (a dbt `schema.yml`, a table comment).
- **People own the annotations**: projects and `uses`, privacy fields, `created_by`, `references`, `instances`,
  and documents. Imports keep them (see above), and annotation commands change only the field they name, keeping
  everything else the entry says (`merge`).
- **Concurrent writes are safe**: an import holds every lock stripe for its one transaction; an annotation waits
  for it, then writes on top of the imported revision, which the next import keeps.

One gap remains: a person's description of an *imported* table is replaced by the next non-merging import. Write
it in the source, or in a document that describes the table (documents are never touched by table imports).

### Running it

```sh
# once, with the database's credentials
odcl migrate
odcl --as you users add you --role admin --email you@example.com
odcl users add etl --role importer --display-name "Scheduled imports"
odcl users token etl --label scheduler          # into the scheduler's secrets as ODCL_TOKEN

# the server people use
ODCL_OIDC_ISSUER=... ODCL_OIDC_CLIENT_ID=... ODCL_OIDC_CLIENT_SECRET=... \
ODCL_OIDC_ROLE_CLAIM=roles ODCL_OIDC_ROLE_VALUES=Odcl.Editor=editor,Odcl.Admin=admin \
  odcl serve --host 0.0.0.0 --accept-lineage

# a person's assistant, with a token from the Account page
ODCL_TOKEN=odcl_... odcl mcp --server https://catalog.example.com --allow-writes
```
