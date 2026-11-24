# Serving, the web page and sign-in

`odcl serve`: the web page, the JSON API, the Docker image, users and single sign-on.

## Serving over HTTP, and the web page

```sh
uv run odcl serve            # http://127.0.0.1:8080/
```

The web page has a page each for Tables (filtered by layer and system), Projects, Model versions and Runs. Each entry has its own page with its lineage graph, columns, the projects that use it and its history. Wherever the page shows a Google resource (a BigQuery table, snapshot or job, a Dataform invocation, a Vertex AI pipeline run, a Cloud Storage object or folder) it links to it in the Google Cloud console. A project's page lists its model versions, and each model version and dataset version is traced back to the raw columns. Columns are found by search or from their table. The same server answers a JSON API, and every read returns exactly what the Python operation of the same name returns:

| Route | Answers |
|---|---|
| `GET /v1/search?q=&kind=&layer=` | `find` |
| `GET /v1/entries/{key}` | `get` |
| `GET /v1/entries/{key}/lineage?direction=up\|down\|both` | `lineage` |
| `GET /v1/entries/{key}/used-by` | `used_by` |
| `GET /v1/tables?layer=&system=` | `tables` |
| `GET /v1/projects` | `projects` |
| `GET /v1/projects/{name}/uses?upstream=true` | `uses` |
| `GET /v1/query-history?table=&since=7d&patterns=true` | `table_history` |
| `GET /v1/datasets/{reference}/trace` | `explain_dataset` |
| `GET /v1/models?project=`, `GET /v1/models/{name}/versions/{version}/trace` | `models`, `explain_model` |
| `GET /v1/runs?system=` | `runs` |
| `GET /v1/overview` | `overview` |
| `GET /v1/operations` | every route here, the role it needs and the fields it takes |
| `POST /api/v1/lineage` | an OpenLineage event, only with `--accept-lineage` |

URL-encode keys and references in the path. Errors come back as `{"error": {"code", "message"}}`, with 404 for something not registered and 400 for a request the catalog refuses.

The same server takes annotations from signed-in users (see [Users and sign-in](#users-and-sign-in)): `POST /v1/projects`, `POST /v1/projects/{name}/uses`, `POST /v1/privacy`, `POST /v1/entries/{key}/created-by`, `/references` and `/instances`, and `POST /v1/models/{name}/versions/{version}/relied-on`, each with a JSON body of the same fields as the Python operation. `GET /v1/me` says who is signed in; `/v1/me/tokens` and `/v1/users` manage tokens and users.

Unless the server runs with `--require-sign-in` or single sign-on, anyone who can reach it can read the whole catalog. That is why it listens on 127.0.0.1 unless `--host` says otherwise. Writes always need a signed-in user whose role allows them: someone who did not sign in is never taken for a direct database connection, so they cannot revoke tokens or read others' alert subscriptions either. OpenLineage events are taken only with `--accept-lineage`, from an importer's API token or, with `ODCL_LINEAGE_TOKEN` set, that shared token. Until the first user is made, an event without a token is accepted, as before users existed, and the server says so when it starts; after that, one is refused. A request the server fails on gets a 500 with a short message, its details in the server's log, and a client has 30 seconds to send a request.

**Behind a proxy.** Set `ODCL_PUBLIC_URL` to the address people use (`https://catalog.example.com`). A browser's change is accepted only from that address (its `Origin`), which a proxy that rewrites `Host` would otherwise break; alerts link to it too. On an address others reach, single sign-on also needs `ODCL_OIDC_REDIRECT_URI` set to its `https://` address, so the session cookie is sent only over TLS; the server refuses to start without it. `ODCL_SESSION_SECRET` signs the cookie that holds a sign-in under way; without it, a key is made when the server starts, and a restart in the middle of a sign-in asks for it again. The page runs only its own script, under a content security policy, and displays catalog text as text.

**The command palette** opens with ⌘K (Ctrl K), with `/`, or from the button beside the search box. Typing searches the catalog; `t:`, `c:`, `p:`, `d:` and `g:` keep it to tables, columns, projects, documents or glossary terms. The entry selected shows what it is, and answers in place who uses it and what changed; Tab reaches those, and copying its name, exploring its lineage and opening it in BigQuery. With nothing typed it lists the entries you opened last and the pages. `>` lists commands: the writes your role allows, each with a form of the fields it takes, run under your name. They are read from `GET /v1/operations`, so an operation added to the table is a command here too. Esc leaves a form, then the palette.

**The lineage explorer** (`#/lineage/KEY`, "Explore →" beside an entry's lineage) draws every edge around a table or column, not only the first path to each entry, so where inputs meet or one input feeds many outputs shows. Upstream is to the left and downstream to the right, in columns by steps from the focus. Choose the direction, how many steps, columns or tables (a table's view folds its columns' lineage in), and whether to show the projects that use what is drawn. Hovering an entry lights what feeds it and what it feeds; clicking one shows it with links to open it or explore from it; typing highlights entries by name. Dotted edges are copies of a definition; dashed amber edges were redefined after the link was recorded. It reads `GET /v1/entries/{key}/graph?direction=&depth=&projects=&level=` (`Catalog.graph`), which returns the nodes and every edge, the nearest 400 at most.

### In Docker

Each release is also an image, `ghcr.io/lawwu/open-data-context-layer`, for `linux/amd64` and `linux/arm64`: Debian 13 and Python 3.14 with every extra installed, run as an unprivileged user (uid 1000). The catalog stays in PostgreSQL, so give it `ODCL_DATABASE_URL`:

```sh
docker run -d --name odcl -p 127.0.0.1:8080:8080 \
  -e ODCL_DATABASE_URL=postgresql://odcl:secret@db.example.com/odcl \
  ghcr.io/lawwu/open-data-context-layer:0.2
```

It runs `odcl migrate` before serving (`-e ODCL_MIGRATE=false` to do that yourself) and listens on every interface inside the container, so `-p` decides who reaches it. Every setting is an environment variable: `ODCL_PUBLIC_URL`, the `ODCL_OIDC_*` ones for single sign-on, `ODCL_SMTP_*` for alerts, `ODCL_LINEAGE_TOKEN`. Options go after the image, replacing the default command: `… open-data-context-layer:0.2 serve --host 0.0.0.0 --require-sign-in`. Any other command runs the same way, or inside the running container:

```sh
docker exec odcl odcl users add alice --role admin
docker run --rm -e ODCL_DATABASE_URL=… ghcr.io/lawwu/open-data-context-layer:0.2 find orders
```

Importing from BigQuery or Vertex AI in a container needs Google credentials in it, such as a service account key mounted read-only with `GOOGLE_APPLICATION_CREDENTIALS` pointing at it. The image reports itself healthy once `GET /v1/me` answers, which needs the database. Tags: `0.2.0` for that release exactly, `0.2` for the newest 0.2.x, `latest` for the newest release, and `sha-…` for a commit. Each image carries its build provenance and an SBOM (`docker buildx imagetools inspect ghcr.io/lawwu/open-data-context-layer:0.2.0 --format '{{json .Provenance}}'`).

## Users and sign-in

odcl has users, each with one role: `reader` reads; `editor` also annotates (projects and their uses, privacy fields, `created_by`, `references`, copies, documents relied on); `importer` also imports (every `odcl import`, builds, model versions, query history, OpenLineage); `admin` also manages users. [Working with several people](working-with-several-people.md) says why: importers run centrally under a service user, and people annotate as themselves.

```sh
odcl --as you users add you --role admin --email you@example.com   # the first admin, over a direct connection
odcl users add etl --role importer --display-name "Scheduled imports"
odcl users token etl --label scheduler      # shown once; give it to the scheduler as ODCL_TOKEN
odcl users set ben --role editor            # or --disable, which stops their tokens and sessions
```

A caller proves who they are with an API token (`odcl_...`, kept only as a digest): `$ODCL_TOKEN` for the CLI and `odcl mcp`, `Authorization: Bearer odcl_...` over HTTP, or pasted once into the web page. A direct database connection without a token is trusted as before: its credentials are the gate. Anyone signed in makes and revokes their own tokens on the web page's **Account** page (or `POST /v1/me/tokens`); an admin also sees and changes every user's role there.

### Single sign-on

With single sign-on through your organization's identity provider, nobody reads the server without signing in: the page sends you to the provider and back, and the API needs that session or an API token. It works with any OpenID Connect provider (Microsoft Entra ID, Okta, Google Workspace, Auth0, Keycloak) and needs the `sso` extra (`uv sync --extra sso`, for PyJWT). It is the authorization code flow with PKCE, a state and a nonce; the provider is found by discovery and its ID token checked (signature against the provider's keys, issuer, audience, expiry, nonce). Nothing of the provider's is kept: the server runs its own session, an HttpOnly, `SameSite=Lax` cookie whose id the database keeps only as a digest.

Register odcl with the provider as a web app using the authorization code flow, with the redirect URI `https://YOUR-SERVER/auth/callback`, then set:

| Setting | |
|---|---|
| `ODCL_OIDC_ISSUER` | The provider's issuer URL (Entra: `https://login.microsoftonline.com/<tenant-id>/v2.0`) |
| `ODCL_OIDC_CLIENT_ID`, `ODCL_OIDC_CLIENT_SECRET` | The app's ID and secret |
| `ODCL_OIDC_DEFAULT_ROLE` | Optional: the role of someone signing in for the first time, `reader` (default), `editor` or `importer` |
| `ODCL_OIDC_ROLE_CLAIM`, `ODCL_OIDC_ROLE_VALUES` | Optional: a claim the provider sends, such as `roles`, and which of its values give which role, e.g. `Odcl.Editor=editor,Odcl.Admin=admin`. The highest wins |
| `ODCL_OIDC_ADMIN_EMAILS` | Optional: who becomes an admin on first sign-in, by an email the provider says is verified (`email_verified: true`, or Microsoft Entra's `xms_edov` optional claim). An email the provider does not vouch for counts for nothing: Microsoft Entra sends no `email_verified`, and its `email` can be set by users, so add the `xms_edov` optional claim or use a role claim instead |
| `ODCL_OIDC_REDIRECT_URI` | Optional: the callback, when the server cannot tell its own address (behind a proxy that ends TLS, set it to the `https://` address) |
| `ODCL_OIDC_POST_LOGOUT_REDIRECT_URI` | Optional: where signing out ends up (default: the server's `/`) |
| `ODCL_OIDC_SCOPES` | Optional: default `openid profile email` |
| `ODCL_OIDC_SESSION_HOURS` | Optional: how long a browser stays signed in, default 12 |
| `ODCL_OIDC_TOKEN_IDLE_DAYS` | Optional: a single-sign-on user's API tokens stop working after this many days without signing in, default 30 (`0`: never) |

All three of the first settings, or none. `odcl serve` prints `single sign-on: <issuer>` when it is on.

- **Users** are found by the provider's issuer and subject, which never change. The first sign-in makes the user, named from their username or email (`ann.lee@example.com` becomes `ann.lee`; `ann.lee-2` if taken), or takes over a user an admin made beforehand with the same email, only when the provider says that email is verified. Roles are decided then and never changed by a later sign-in, so a provider's claims changing cannot silently promote anyone; change them with `odcl users set` or on the Account page.
- **API tokens** of single-sign-on users stop working after `ODCL_OIDC_TOKEN_IDLE_DAYS` without signing in, so people removed from the provider lose API access too. Signing in again brings them back. Service users made with `odcl users add` keep their tokens.
- **A browser session changes nothing from another site**: a write made with the session cookie must come from the server's own page (its `Origin`).
- **Signing out** (on the Account page) ends the session, and the provider's too where it offers that (`end_session_endpoint`).
