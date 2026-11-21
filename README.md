# Open Data Context Layer

A data context layer: what data exists, how it connects, and which projects use it. It is for people and for AI agents, so both can find the right data and see what a change would affect.

The catalog holds descriptions. It does not store or serve the data itself. Everything it keeps lives in one PostgreSQL database.

The [implementation plan](docs/implementation-plan.md) describes where it is going. Its first goal, a model trace from a Vertex AI Pipelines training run back through Dataform and BigQuery to the raw columns, is built and has been run against a real project ([`examples/live_trace/`](examples/live_trace/)).

## What it keeps

| Entry | What it is | Key |
|---|---|---|
| table | A table, view or file in some system | `table:<database>.<schema>.<name>` |
| column | One column of a registered table | `column:<table path>.<name>` |
| project | Anything that uses data: an analysis, a dashboard, a model | `project:<name>` |
| document | A Markdown page that describes tables, columns or projects | `document:<repo>/<path>` |

Data is registered once and shared. It does not belong to a project. Two links connect entries:

- **derived_from**: what a table or column is built from (lineage).
- **uses**: the tables and columns a project used, with what for.

A `uses` link remembers the revision of the data the project saw. If the data is redefined later, the catalog says so until the project records use again.

Every change is a new revision with who made it and when.

Around these entries the catalog also keeps what ran and what it made:

- **Runs**: Dataform actions, Vertex AI training tasks, runs reported over OpenLineage, and the BigQuery jobs behind them. Runs are only ever added.
- **Dataset versions**: a snapshot one build handed on, with each field pinned to the column definition that build used.
- **Model versions**: one trained version of a model, written once. A model is a project. Each version belongs to its project and reads one dataset version.

A model version's pins reach its project's `uses` links. Importing the newest version of a model records that its project uses the columns that version was trained on, at the definitions it used, so `used-by` on a raw column reaches the model. If someone redefines a feature after training, the project is flagged until a version trained on the new definition is imported.

## Try it

You need Python 3.14 or later and a PostgreSQL database the catalog may create its tables in.

```sh
uv sync --all-extras
createdb odcl
export ODCL_DATABASE_URL=postgresql://localhost/odcl
uv run odcl migrate
uv run odcl import --dbt tests/fixtures/dbt_medallion/target
uv run odcl add project churn-model --description "Predict which accounts cancel within 90 days"
uv run odcl use churn-model churn.main_gold.account_features.plan_tier --for "training feature"
uv run odcl find plan
uv run odcl used-by churn.bronze.accounts.plan
uv run odcl uses churn-model --upstream
uv run odcl lineage churn.main_gold.account_features.tenure_months
uv run odcl serve   # then open http://127.0.0.1:8080/
```

`used-by` on the bronze `plan` column reports `churn-model`, because the gold column the project uses is built from it:

```text
column:churn.bronze.accounts.plan is used by 1 project(s)
  project:churn-model  downstream through column:churn.main_gold.account_features.plan_tier  for: training feature
```

A project depends on data in one of four ways, strongest first: `direct` (it used this entry), `column` (it used a column of this table), `table` (it used the table this column is in), `downstream` (it used something built from this).

## Commands

| Command | Answers |
|---|---|
| `odcl find WORDS [--kind] [--layer] [--retired]` | What data is there for this? |
| `odcl show KEY` | Everything about one entry, with its history |
| `odcl lineage KEY` | What is it built from, and what is built from it? |
| `odcl diff KEY [--since REV\|TIME] [--until REV\|TIME]` | What changed, and does it break what uses it? |
| `odcl import --quality FILE.jsonl` or `--dbt-tests TARGET`; `odcl quality KEY [--at TIME]` | Was it passing its tests, now or then? |
| `odcl term add`, `odcl domain add`, `odcl means`, `odcl in-domain`, `odcl glossary` | What does this column mean in the business's words, and whose is it? |
| `odcl alerts subscribe\|list\|remove\|send` | Tell the people behind a project when data it uses changes or goes |
| `odcl used-by KEY` | Which projects depend on it? |
| `odcl uses PROJECT [--upstream] [--revision REV]` | What did this project use, now or at an older revision of it? |
| `odcl use PROJECT KEY... [--for TEXT]` | Record that a project used data |
| `odcl add table\|column\|project ...` | Register by hand |
| `odcl import --dbt PATH [--select MODEL...]` | Read a dbt project |
| `odcl import --bigquery TABLE...` | Read tables and their columns from BigQuery |
| `odcl import --postgres URL [--schema S...]` | Read a PostgreSQL database's tables, views, view lineage and foreign keys |
| `odcl sync bigquery-jobs --project PROJECT [--since T --until T \| --pipeline-run JOB [--task T]]` | Read the query jobs a project ran since the last sync, or in one window |
| `odcl history table KEY [--since 7d] [--patterns]` | Which query jobs read or wrote this table? |
| `odcl import --dataform INVOCATION` | Read what a Dataform run compiled and how each action ran |
| `odcl handoff --invocation INVOCATION --target TABLE --snapshot SNAPSHOT` | Record that a build handed on a snapshot of its output |
| `odcl explain dataset SNAPSHOT[@TIME]` | Which build, job, file and inputs a dataset version came from |
| `odcl record dataset-files --pipeline-run JOB --task TASK --output NAME [--from-table TABLE] [--export-job P:L.JOB \| --find-export-job]` | Record the files a pipeline task exported as a dataset version, bound to the BigQuery job that wrote them |
| `odcl import --sql-definitions FILE.jsonl` | Read tables and the SELECT that builds each, from any source |
| `odcl created-by TABLE SCRIPT\|NOTEBOOK\|URL [--command] [--note]` | Say what created a table or file |
| `odcl references COLUMN TARGET...` | Say which column a column's values are drawn from (a foreign key) |
| `odcl instances DEFINITION PATTERN...` | Say which tables are copies of a definition, so lineage and users go through them |
| `odcl privacy set KEY... [--pii] [--data-category] [--retention] [--scrubbing]` | Say which data is personal, how it is scrubbed and kept |
| `odcl privacy export [--pii-only] [--table T]` | Every column with its privacy fields, as CSV |
| `odcl discover vertex-runs --project P` | Which pipeline runs have a model version recorded? |
| `odcl propose vertex-manifest --pipeline-run JOB [--out FILE]` | Draft a run's training manifest for review |
| `odcl rely MODEL --version V DOCUMENT...` | Record that a model version relied on documents, at the revision it was trained on |
| `odcl import --vertex-manifest PATH\|gs://...` | Check a training manifest against Vertex AI and Cloud Storage, and record the model version |
| `odcl explain model NAME --version V` | Which run, artifact, dataset version and raw columns a model version came from |
| `odcl overview` | Counts, and data that changed after a project used it |
| `odcl migrate` | Create or update the catalog's tables |
| `odcl import --openlineage FILE` | Read OpenLineage events: runs and the lineage they establish |
| `odcl import --docs PATH... [--repo-url URL] [--commit SHA]` | Read Markdown pages that describe catalog entries |
| `odcl docs KEY` | Which documents describe this entry? |
| `odcl serve [--accept-lineage] [--require-sign-in]` | Serve the catalog over HTTP, with a web page to browse it |
| `odcl mcp [--allow-writes] [--server URL]` | Serve all of this to an assistant |
| `odcl users list\|add\|set\|token\|tokens\|revoke\|whoami` | Who may read and write, and their API tokens |

Add `--json` before the command for the full result. A bare path such as `churn.bronze.accounts` works wherever a key does, when only one entry has it.

The database is one PostgreSQL database: `--database-url`, else `$ODCL_DATABASE_URL`. Run `odcl migrate` once on a new database and again after upgrading. With `$ODCL_TOKEN` set to an API token, the command signs in as that token's user, records changes under their name and checks their role (see [Users and sign-in](#users-and-sign-in)). Without one it is a direct connection, trusted because it holds the database's credentials, and changes are recorded under `--as`, else `$ODCL_USER`, else your login name.

## Importing from dbt

`odcl import --dbt target/` reads `manifest.json` (run `dbt compile` or `dbt run` first), and `catalog.json` for column types when `dbt docs generate` has written it.

- Every source and model becomes a table, with its layer (from `meta.layer`, a bronze/silver/gold tag, or the schema name), owner (`meta.owner`) and description.
- Table lineage comes from dbt's dependency graph. Column lineage is read from each model's compiled SQL with [sqlglot](https://github.com/tobymao/sqlglot).
- Each entry records the file that defines it and the commit that last changed that file, with a link when the repository has a web address.
- Importing again changes nothing unless something changed. Redefining a table does not create new revisions of everything built from it; those entries report that their input changed.
- A column that sqlglot cannot trace fully (a row count, for example) is tagged `lineage:partial`.

## Importing from BigQuery

The BigQuery commands need the `bigquery` extra, and read through [Sounding](https://github.com/lawwu/sounding). Sign in once with `gcloud auth application-default login`. Every call is free: `tables.get` for schemas and `jobs.list` for jobs. No query is run.

```sh
uv sync --all-extras
uv run odcl import --bigquery my-project.features.shift_inputs my-project.raw.shift_totals
uv run odcl sync bigquery-jobs --project my-job-project
uv run odcl history table my-project.features.shift_inputs --since 7d --patterns
```

**Schemas.** Each table becomes a table entry and each field a column, nested fields by dotted path (`shift.hours_at_cutoff`). A snapshot records its base table and snapshot time. Row counts and sizes are left out, so importing again changes nothing unless the definition changed.

**Job history.** `sync bigquery-jobs` reads the query jobs created in one project, the project the jobs ran in, which may differ from where the tables live. Reading other people's jobs needs `bigquery.jobs.listAll` on that project.

- The first sync goes back 7 days (`--backfill`, up to the 180 days BigQuery keeps). Each later sync starts 6 hours before the last finished window ended, or at the oldest job still running, and keeps every job once.
- To read one window instead, such as the minutes a pipeline task ran, give `--since` and `--until` (ISO 8601, UTC unless it says; `--since` also takes a span back from now like `12h`), or `--pipeline-run JOB` and optionally `--task NAME` to use the window that run or task reports. Its jobs are kept like any others, but the window is not recorded as read through, so the next regular sync still starts where the last one ended. An `--until` later than now is read as now, so a regular sync is never recorded as read past the present.

  ```sh
  uv run odcl sync bigquery-jobs --project my-job-project --pipeline-run projects/P/locations/L/pipelineJobs/J --task load-data
  ```
- A sync that is interrupted keeps the pages it read but does not record the window as finished, so the next sync reads it again.
- A job that finished is never replaced by an older reading that says it is still running.
- Jobs labelled as a metadata collector's own (`sounding_action`, `odcl_action`) are left out.

**What a job tells you.** A `read` is a table the job reported referencing. A `write` is recorded only for a statement that succeeded and named a lasting destination; a query's temporary result table is not one. Cache hits and failed jobs often report no tables, and are kept with a `no_table_references` gap. A script and its statements are both kept; totals count the statements, not the script as well.

A job shows that a table was queried. It does not show which fields a model was trained on.

**SQL.** By default each query is kept only as its shape: comments removed and every literal and parameter replaced by `?`, so two lookups for different employees are one example. The original SQL is not stored. A script, or SQL that cannot be read for certain, is kept without a shape (`template_unavailable`). `--sql none` does not ask BigQuery for SQL at all. Error messages and user emails are never read.

Grouping uses the catalog's own digest of the shape. BigQuery's normalized query hash is only in `INFORMATION_SCHEMA`, which costs a query to read.

## Importing from PostgreSQL

odcl reads a PostgreSQL database's own catalog: its tables (ordinary, partitioned and foreign), views and materialized views, their columns, and their foreign keys. It reads no rows.

```sh
uv run odcl import --postgres postgresql://reader@db.internal/shop --schema public sales
```

Keep the password out of the URL, where it would land in shell history: set `PGPASSWORD` or use a `~/.pgpass` file, which libpq reads.

- **Keys** are `database.schema.table` (`--database-name` to key them by another name), lower-cased, like every key. The URL, and any password in it, is not kept.
- **Descriptions** are the tables' and columns' comments; each column keeps its type, position and whether it may be null.
- **View lineage.** Each view's SQL is read with every table it reads named by its schema, and each of its columns is traced through it as a Dataform definition is, in PostgreSQL's dialect: through CTEs and `SELECT *`, from views to the tables under them, and tagged `lineage:partial` when only partly traced. Quoted mixed-case names match their lower-cased keys.
- **Foreign keys** become `references` links. A partitioned table's key is read once, not again for each partition. A key to a schema not read is named in `skipped`, as is a name the catalog cannot key (and any key on such a column), and a second table or column whose name differs from another's only in case.
- **Tables in schemas not read**, that a view reads, are registered by name and named in `skipped` on every import until something registers them. The column names a view's SQL gives them are not taken as their full list, so importing again traces the view the same way.
- Entries merge with what other importers said, under `postgres` in `source`, and are applied together. Importing again changes nothing unless the database changed.

## Importing from Dataform

```sh
uv sync --extra dataform
uv run odcl import --dataform projects/P/locations/L/repositories/R/workflowInvocations/I
```

This reads one workflow invocation and the compilation result it ran, with free Dataform calls. A definition and a run are kept apart:

- **Definitions.** Every table the compilation declares or builds becomes a table entry, with its file and the tables it reads. Every column the compiled query selects becomes a column entry with its expression and the columns it is computed from, traced by [sqlglot](https://github.com/tobymao/sqlglot). A column it cannot trace fully is tagged `lineage:partial`; SQL it cannot read marks the table's columns partial and drops their old definitions.
- **Runs.** The invocation and each action are recorded as executions: state, times, and the BigQuery job ID. The job itself is looked up in synced query history when a trace is read, so `odcl sync bigquery-jobs` may run before or after the import.
- **Shared entries.** BigQuery and Dataform describe the same physical tables and columns, so they share keys. Each import adds what it knows (BigQuery the types, Dataform the expressions) and keeps what the other said. Each keeps its own details under its name in `source`, tags included: an import replaces only the tags that importer set before, and keeps tags people and other importers added.
- **Order.** An invocation's definitions are applied only if its compilation is at least as new as the newest already imported from that repository. An older invocation imported late still has its runs recorded, without a pinned definition, and its trace says so.

Import an invocation by name for now. Discovering invocations is not built yet.

## Importing SQL definitions from anywhere

A registry, a hand-kept list of views, or any tool odcl has no importer for can write its definitions as JSON Lines, one table per line, and odcl reads them the way it reads a Dataform compilation: each column is traced through the SELECT, `SELECT *` expands to what the catalog knows of the tables read, and a column traced only in part is tagged `lineage:partial`.

```json
{"target": "proj.ds.orders", "declaration": true, "description": "Orders, as the billing system loads them"}
{"target": "proj.ds.by_region", "type": "view", "file": "views/by_region.sql", "description": "Totals by region", "select": "SELECT region, SUM(amount) AS total FROM proj.ds.orders GROUP BY region", "dependencies": [], "columns": {"total": "Order amount, summed"}}
```

```sh
uv run odcl import --sql-definitions definitions.jsonl            # --dialect for SQL other than BigQuery's (--system if not the dialect's name)
```

`target` is required, and so is `select` unless the line is a `declaration` (a source table with no SQL). `dependencies` adds tables the SELECT does not name. Entries merge with what other importers said, under `sql_definitions` in `source`; the whole file is applied together, and a line that cannot be read is refused with its line number before anything is written.

The result names what could not be linked rather than failing: `unresolved`, the tables read that are neither defined in the file nor registered (registered by name only), and `untraced`, the targets whose SQL sqlglot cannot read. A Dataform import reports the same two lists.

## When something is gone from its source

An import also says what is no longer there. After it registers what it read, whatever the same importer registered before, within what it read this time, and did not see is withdrawn, in the same transaction:

| Import | What it read, so what it can retire |
| --- | --- |
| `--postgres` | the database's schemas it read (all of them, without `--schema`): tables, views and their columns |
| `--sql-definitions FILE` | the definitions from that file (or `--repository`), and their columns |
| `--dataform` | the repository's declarations and tables, when its definitions are applied |
| `--dbt` | the dbt project's models and sources; nothing with `--select`, which reads only part |
| `--bigquery TABLE…` | the columns of the tables named; never a table, since only those are read |

- **Withdrawn or retired.** Withdrawing removes the importer's part of an entry, under its name in `source`, with the tags it set. If another importer still says the entry (BigQuery and Dataform describe the same tables), that is all. If nothing else does, the entry is **retired**: a new revision marked `retired`, with no links. Tables and columns registered only because a query named them are left alone.
- **What retirement changes.** A retired table or column leaves `odcl find`, `odcl tables` and the counts (`--retired`, or `retired=1` over HTTP, includes it), and a table lists its retired columns apart. A project that used it is listed in `overview` as `retired_but_used` and flagged on the web page. Its history keeps every revision, so what a model version pinned can still be read.
- **Coming back.** When the importer sees it again, it is registered as usual and no longer retired. A hand edit, such as a description, does not revive it.
- Importing again changes nothing: what is retired stays retired, and each import's result lists what it `retired` and `withdrawn`.

## What changed, and how much it matters

`odcl diff` compares an entry between two points in its history, and a table's diff includes its columns, since a table's schema is its columns. Each change is labelled by how much it matters to what uses it:

| Severity | Changes |
| --- | --- |
| `breaking` | a column's type changed from one stated type to another; a table or column removed or retired |
| `semantic` | a definition, the inputs it is computed from, a foreign key, the copies of a definition, a privacy field, a document's text |
| `additive` | a column or table added, or revived |
| `cosmetic` | a description, owner, tags, layer, a type stated for the first time |

```sh
uv run odcl diff core.sales.daily_totals                 # its latest change, with its columns'
uv run odcl diff core.sales.daily_totals --since 7d      # or a revision (rev_...), or a time
```

What a project uses carries the worst change since it used it (`severity` on `get` of a project, `uses` and `overview`'s `changed_since_used`), measured from when the use was recorded. A used table counts as changed when one of its columns changed, even if the table's own revision did not. The same diff is `GET /v1/entries/{key}/diff?since=&until=`, the `what_changed` MCP tool and `Catalog.diff`, and an entry's page shows it, with "changes since" on each revision in its history.

## Glossary terms and domains

A **term** is a word the business uses, with its definition: `net-revenue`, "revenue after refunds and discounts". A **domain** is a part of the business that owns data, projects and terms: `sales`. Both are entries, with revisions and history like any other, and both can be described by documents.

```sh
uv run odcl domain add sales --description "Selling, from quote to cash" --owner ana
uv run odcl term add net-revenue --definition "Revenue after refunds and discounts" --synonym "net sales" --domain sales --status approved
uv run odcl means core.sales.daily_totals.amount net-revenue      # what a table or column means; no terms clears them
uv run odcl in-domain core.sales.daily_totals sales               # a table, project or term; no domain takes it out
uv run odcl glossary [--domain sales]
```

- A column or table `means` terms; a table, project or term is `in_domain` one domain. A term's page lists what means it; a domain's page lists its tables, projects and terms; search finds a term by its synonyms. The web page has a Glossary.
- These links are said beside what importers say, so an import, merged or not, keeps them. A change to them is `cosmetic` in `odcl diff`.
- Over HTTP: `GET /v1/glossary`, and as an editor `POST /v1/terms`, `/v1/domains`, `/v1/entries/{key}/terms`, `/v1/entries/{key}/domain`. `find_data` searches terms and domains.

## Data quality

odcl runs no tests. It keeps what your tests said, pinned to the revision of the table or column that was current when each ran, so "was this column failing when the model trained on it?" has an answer.

```sh
uv run odcl import --dbt-tests target/                       # run_results.json and manifest.json, after dbt test or dbt build
uv run odcl import --quality results.jsonl --source great_expectations
uv run odcl quality core.sales.daily_totals                  # the newest result of each test, on the table and its columns
uv run odcl quality core.sales.daily_totals --at 2026-11-01  # as they stood then
```

Anything that can write a line of JSON per result works, such as Great Expectations, Dataform assertions, Soda or a notebook:

```json
{"entry": "core.sales.daily_totals.amount", "test": "amount_not_negative", "status": "fail", "failures": 12, "observed_at": "2026-11-20T06:00:00Z", "run": "nightly-2026-11-20", "kind": "expression", "message": "12 rows below 0"}
```

- A status is `pass`, `warn`, `fail`, `error` or `skip`. A result is kept once per source, run, test and entry, so importing again changes nothing. An entry that is not registered is named in `unresolved`, not refused; a malformed line refuses the whole file.
- Each result pins the revision current when the test ran. A result from before the entry's latest revision says so (`current: false`).
- An entry's page and `get` show its newest results, a table with its columns'. `overview` lists `failing_and_used`: projects that use a column failing a test, or its table. A model version's trace lists `quality_at_training`: tests failing, when it was trained, on the columns its inputs came from and their tables.
- Over HTTP: `GET /v1/entries/{key}/quality?at=` and `POST /v1/quality` (importer). Run `odcl migrate` for migration 0013.

## Alerts

The overview lists what changed after a project used it; alerts tell the people behind the project. A subscription covers one project, the projects of one owner (their `owner` field), or every project, and sends to a webhook, a Slack incoming webhook or an email address, for changes of a severity and above (`semantic` by default):

```sh
uv run odcl alerts subscribe --project forecast --slack https://hooks.slack.com/services/…
uv run odcl alerts subscribe --owner ana --email ana@example.com --min-severity breaking
uv run odcl alerts subscribe --all --webhook https://ops.example.com/odcl
uv run odcl alerts send            # after imports, from the same scheduler; --dry-run to see what would go
```

- **What is sent.** An entry a project uses that changed after the use (with each change and its severity, as `odcl diff` gives them), or that is retired. A used table changes when its columns do. One message per subscription and run; the webhook's JSON has every change, with `project`, `key`, `kind` (`changed` or `retired`), `severity` and `changes`.
- **Once each.** A delivery is recorded per subscription, project, entry and the entry's newest revision (a table's newest column counts), so a change is sent once, and a later change to the same entry is sent again. Recording use again (`odcl use`) clears it. A failed delivery is retried on the next `send`.
- **Who.** An editor subscribes and removes their own subscriptions; an admin sees and removes all of them. `send` needs an importer, like the imports it follows. A webhook address is `https://` (or `http://` on this machine); its path, often its secret, is not shown when subscriptions are listed. Anyone with the database's credentials can read the addresses.
- **Email** goes through `$ODCL_SMTP_HOST` (`$ODCL_SMTP_PORT`, default 587 with STARTTLS; `$ODCL_SMTP_USER`, `$ODCL_SMTP_PASSWORD`; `$ODCL_SMTP_FROM`). With `$ODCL_PUBLIC_URL`, each alert links to the entry's page.
- Over HTTP: `GET`/`POST /v1/alerts/subscriptions`, `POST /v1/alerts/subscriptions/{id}/remove`, `POST /v1/alerts/send`. Run `odcl migrate` for migration 0012.

## Binding data to the build that made it

A table name and its latest successful job do not say which run produced the rows a model read. In the pilot a build hands on a snapshot instead: after the action that writes a table succeeds, and before the next write, it creates a uniquely named [table snapshot](https://docs.cloud.google.com/bigquery/docs/table-snapshots-intro) and records the handoff:

```sh
uv run odcl handoff --invocation projects/P/locations/L/repositories/R/workflowInvocations/I \
  --target my-project.features.shift_inputs --snapshot my-project.frozen.shift_inputs_b042
uv run odcl explain dataset my-project.frozen.shift_inputs_b042
```

`handoff` reads the snapshot from BigQuery (it needs the `bigquery` extra) and refuses it unless the action succeeded, the snapshot copies the action's target, it was taken after the action ended, and synced history shows no other write to the target in between. It stores the snapshot as a dataset version, with each field pinned to the column definition the build used. Recording the same handoff again changes nothing; claiming the snapshot for another run is refused.

`explain dataset` answers from those pins, not from today's definitions:

```text
workforce-demo.frozen.shift_inputs_b042@2026-09-02T02:00:00Z
  snapshot of   workforce-demo.features.shift_inputs
  produced by   projects/workforce-demo/.../workflowInvocations/build-042 / workforce-demo.features.shift_inputs  SUCCEEDED
  executed as   BigQuery job (workforce-demo, us-central1, job-042)  CREATE_TABLE_AS_SELECT
  defined in    definitions/shift_inputs.sqlx at commit 91fa3c5be2d04a6c8f7e1b0d9a2c4e6f8a1b3c5d
  hours_at_cutoff = worked_minutes_asof / 60  from workforce-demo.raw.shift_totals.worked_minutes_asof
```

Anything it could not establish is listed under `missing`: a job not yet in synced history, a field traced only in part, history that does not reach the snapshot time, a definition compiled from a workspace rather than a git commit. This traces the definition of each field back to its raw columns; it does not keep the raw rows.

A Dataform action runs as a BigQuery script, and the statement that writes the table is a child job of it. The build's own statements count as the build, not as another write.

### When the training data is files, not a snapshot

Many pipelines train on files instead: a data-loader task exports Parquet (`output_train_dataset`, `output_val_dataset`, …) and the trainer reads those. The table they came from may have been re-created since, so no snapshot can be taken. Record the files instead:

```sh
uv run odcl record dataset-files --pipeline-run projects/P/locations/L/pipelineJobs/J --task load-data \
  --output output_train_dataset --from-table my-project.features.shift_inputs
uv run odcl explain dataset gs://bucket/.../output_train_dataset@J
```

- **Which files.** The command reads the task's output artifact URI from the pipeline job, which must have succeeded. It lists every object under that URI, skipping empty markers such as `_SUCCESS`, and keeps each one with its object generation.
- **Reference.** The file set's reference is the artifact URI and the job, `gs://…/output_train_dataset@J`.
- **Files not written by a pipeline task** can be named directly with `--uri gs://…`. Their reference ends in the newest object's generation, and the trace says their producer is not known: a producer is never guessed.
- **Fields** come from each object's Parquet footer, read with a range request, so no file is downloaded. If the footers cannot be read, or the objects disagree, the file set is recorded with no fields and says why. A manifest that names fields is then refused with that reason.
- **Recording again.** Recording the same files again changes nothing. The same reference naming other bytes is refused.
- **`--from-table`** records the table the files were exported from, as table-level lineage only. Its definition is pinned as it is when the files are recorded, and the trace says the table may have changed between writing the files and recording them.

**Binding the files to the BigQuery job that wrote them.** A data-loader task usually writes its files with one `EXPORT DATA … AS SELECT` statement. That SELECT is the definition of each file column, and matching fields to table columns by name would be wrong when a column is renamed (`label AS target_regression`). Name the job, or let odcl find it:

```sh
uv run odcl record dataset-files --pipeline-run JOB --task load-data --output output_train_dataset --export-job my-project:US.job_abc123
uv run odcl record dataset-files --pipeline-run JOB --task load-data --output output_train_dataset --find-export-job
```

- **Checks.** The job is read with `jobs.get`, which is free. It is accepted only if it is an EXPORT DATA statement that succeeded, wrote under the file set's location, and was created before the files were. `--find-export-job` lists the jobs created while the task ran (`jobs.list`, also free) and refuses if none matches or several do, naming them.
- **Each field** the SELECT outputs is traced with sqlglot, as Dataform actions are: to the catalog columns it is computed from, with its expression, `resolved` or `partial`. A field the SELECT does not output stays not claimed.
- **Source tables** must be in the catalog; one that is not is named and nothing is recorded.
- **Row filters.** The WHERE clause's columns are recorded as the version's row filters.
- **Pinning.** Each source column is pinned to the revision the catalog recorded before the job ran when there is one. Otherwise it is pinned as it is now, and the trace says the definition may differ.
- **Whether the files are the job's own output.** The job's file and row counts are compared with the Parquet objects recorded and the row counts in their footers. Other objects in the folder, such as a `metadata.json` sidecar, are pinned with the file set but not counted, and the trace names them. Different row counts are refused. Different file counts with equal rows (a task that compacted the files afterwards) are bound, and the trace says the files were rewritten after the export.
- **What is kept.** The job's SQL is kept as its digest and its shape, or only its digest with `--sql none`. Binding the same job again changes nothing, and a file set is bound once. One recorded without a binding can be bound by recording it again with `--export-job`.

A bound file set proves which query produced its rows and each column's expression. It does not prove the source table's definition at the moment the job ran, unless the catalog recorded it before then, or what any later step did to the files. A model trained on it traces from each field to its source columns and on down the catalog's lineage, and its project's `uses` link points at those columns.

What a file set proves, and what it does not: it proves the exact bytes the trainer read, because each object is pinned to a generation and checked again when a model is imported. It does not prove which build produced those bytes, and it claims no column lineage below them. The trace lists both under `missing`.

## Model versions from Vertex AI Pipelines

The training task writes a manifest after it uploads the model: which pipeline run and task it is, the dataset version it read, the fields of the training matrix in order with each one's role (`feature`, `label`, `cohort_filter`, `join_key`, `weight`), and the artifact's Cloud Storage URI, object generation and SHA-256. [`odcl.manifest`](src/odcl/manifest.py) writes one and has no dependencies, so training code can vendor that file. Take the fields from the matrix as fitted, not from configuration.

```sh
uv sync --extra vertex
uv run odcl import --vertex-manifest gs://my-bucket/models/staffing-forecast/v17/manifest.json
uv run odcl explain model staffing-forecast --version v17
```

The manifest is the trainer's claim, so the import checks it before writing anything, with free reads (`pipelineJobs.get` and Cloud Storage object reads):

- The pipeline run has the named task and the task succeeded.
- The artifact is still the object generation the manifest names, and its bytes hash to the manifest's SHA-256 (artifacts over 512 MB are checked by generation only).
- The dataset version is recorded (by `odcl handoff`, or `odcl record dataset-files` for files), and each field is a column of it. A missing one is named, and nothing is written until it is there.
- For a file set, every object still has the generation recorded. An object replaced or deleted since is named, and nothing is written.
- Importing the same manifest again changes nothing. A different manifest for a version already recorded is refused: a model version is written once.

The model's project is registered at its first version if it is not already. If the version is the project's most recently trained one, its project's `uses` links then point at the columns it was trained on, pinned to the definitions it used and noted with each field's role (`feature in v17`). Links a person recorded with `odcl use` are kept. A version imported late, after a newer one, does not move them back. Replaying the manifest of a version recorded by an earlier release fills its links in.

`explain model` follows the pins all the way down:

```text
staffing-forecast / v17
  produced by   projects/workforce-demo/locations/us-central1/pipelineJobs/train-042 / task train
  artifact      gs://demo-models/staffing/v17/model.bin#1788318000000000  sha256 0402ca8883ee…
  read          workforce-demo.frozen.shift_inputs_b042@2026-09-02T02:00:00Z, snapshot of workforce-demo.features.shift_inputs
  feature  hours_at_cutoff = worked_minutes_asof / 60
           built by projects/workforce-demo/.../workflowInvocations/build-042
           as BigQuery job (workforce-demo, us-central1, job-042), from definitions/shift_inputs.sqlx at commit 91fa3c5be2d04a6c8f7e1b0d9a2c4e6f8a1b3c5d
           from workforce-demo.raw.shift_totals.worked_minutes_asof
```

Redefining the feature and training a later version leaves this answer unchanged. A model trained on a file set traces to the files (how many objects, generations pinned), the task that wrote them and the table they were exported from, and stops there. Its project's `uses` link points at that table. `odcl` reads Google with your Application Default Credentials, which bill each request to their quota project. If that project has no billing, Google refuses with a billing error; set `GOOGLE_CLOUD_QUOTA_PROJECT` to one that does.

### Finding runs and drafting their manifests

A training task that does not write a manifest can still be imported. odcl lists a project's pipeline runs, says which already have a model version recorded, and drafts a manifest for one from what the run left behind:

```sh
uv run odcl discover vertex-runs --project workforce-demo --location us-central1 --since 14d
uv run odcl propose vertex-manifest --pipeline-run projects/P/locations/L/pipelineJobs/J --out manifest.json
uv run odcl import --vertex-manifest manifest.json        # after reviewing it
```

The draft takes the trainer from the task with a Model output, the artifact from the one file in it (its generation and SHA-256 read from Cloud Storage), and the inputs from the `metadata.json` beside it: `feature_columns` in order, the `label_column`, and the model name, version and commit when it says them. The dataset version is the data loader's output, named as `odcl record dataset-files` names it. It says where each value came from and lists what to review: a guess (several tasks that could be the trainer, a version taken from the job id), anything not found, and the files to record first. `--trainer`, `--loader`, `--loader-output`, `--model` and `--version` settle any guess. Nothing is written until the draft is imported, and the import checks it like any other manifest.

## OpenLineage

Any tool with an [OpenLineage](https://openlineage.io) integration (Airflow, Spark, dbt, Dagster, Flink, …) can report its runs to the catalog. Point its HTTP transport at a server started with `odcl serve --accept-lineage`. Events are posted to `/api/v1/lineage`, the same path Marquez uses, so the usual configuration works:

```sh
ODCL_LINEAGE_TOKEN=choose-a-secret uv run odcl serve --accept-lineage
export OPENLINEAGE_URL=http://localhost:8080 OPENLINEAGE_API_KEY=choose-a-secret   # in the reporting tool
```

Or read events saved to a file: one JSON event, a JSON list, or JSON Lines.

```sh
uv run odcl import --openlineage events.jsonl
```

How an event becomes catalog entries:

- **Datasets** become table entries, and the fields of their schema facet become column entries, nested fields by dotted path. A warehouse dataset keeps its name as its key (`bigquery` / `my-project.features.shift_inputs` is `table:my-project.features.shift_inputs`), so it is the same entry the BigQuery and Dataform importers write. Each importer adds what it knows and keeps what the others said. Other datasets, such as files in a bucket, get a key built from their namespace and name, which are kept under `source.openlineage`.
- **Lineage** comes only from a run that completed, or from a job event that declares it. A failed run built nothing, so it adds no lineage. An output table is derived from the run's inputs. An output column is derived from the input fields its `columnLineage` facet marks `DIRECT`. An `INDIRECT` field, such as a filter or a join key, decides which rows count but not the value, so it is not lineage. A transformation's description becomes the column's definition.
- **Runs** are recorded with their job, state, times, inputs, outputs and parent run. Events for one run may arrive in any order, and a run that has finished is not reopened by a late `START`.
- **What is kept.** Each event is stored once, so a replay changes nothing. As with BigQuery jobs, SQL is kept only as its shape, with every literal replaced by `?`, and error messages are dropped.

Sending the catalog's own runs out as OpenLineage events is not built yet.

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
| `POST /api/v1/lineage` | an OpenLineage event, only with `--accept-lineage` |

URL-encode keys and references in the path. Errors come back as `{"error": {"code", "message"}}`, with 404 for something not registered and 400 for a request the catalog refuses.

The same server takes annotations from signed-in users (see [Users and sign-in](#users-and-sign-in)): `POST /v1/projects`, `POST /v1/projects/{name}/uses`, `POST /v1/privacy`, `POST /v1/entries/{key}/created-by`, `/references` and `/instances`, and `POST /v1/models/{name}/versions/{version}/relied-on`, each with a JSON body of the same fields as the Python operation. `GET /v1/me` says who is signed in; `/v1/me/tokens` and `/v1/users` manage tokens and users.

Unless the server runs with `--require-sign-in` or single sign-on, anyone who can reach it can read the whole catalog. That is why it listens on 127.0.0.1 unless `--host` says otherwise. Writes always need a signed-in user whose role allows them. OpenLineage events are taken only with `--accept-lineage`, from an importer's API token or, with `ODCL_LINEAGE_TOKEN` set, that shared token; without either set, and without sign-in required, any sender is accepted, as before. The page runs only its own script, under a content security policy, and displays catalog text as text.

## Users and sign-in

odcl has users, each with one role: `reader` reads; `editor` also annotates (projects and their uses, privacy fields, `created_by`, `references`, copies, documents relied on); `importer` also imports (every `odcl import`, builds, model versions, query history, OpenLineage); `admin` also manages users. [Working with several people](docs/working-with-several-people.md) says why: importers run centrally under a service user, and people annotate as themselves.

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
| `ODCL_OIDC_ADMIN_EMAILS` | Optional: who becomes an admin on first sign-in. Never by an email the provider says is unverified (`email_verified: false`); where people choose their own email, use a role claim instead |
| `ODCL_OIDC_REDIRECT_URI` | Optional: the callback, when the server cannot tell its own address (behind a proxy that ends TLS, set it to the `https://` address) |
| `ODCL_OIDC_POST_LOGOUT_REDIRECT_URI` | Optional: where signing out ends up (default: the server's `/`) |
| `ODCL_OIDC_SCOPES` | Optional: default `openid profile email` |
| `ODCL_OIDC_SESSION_HOURS` | Optional: how long a browser stays signed in, default 12 |
| `ODCL_OIDC_TOKEN_IDLE_DAYS` | Optional: a single-sign-on user's API tokens stop working after this many days without signing in, default 30 (`0`: never) |

All three of the first settings, or none. `odcl serve` prints `single sign-on: <issuer>` when it is on.

- **Users** are found by the provider's issuer and subject, which never change. The first sign-in makes the user, named from their username or email (`ann.lee@example.com` becomes `ann.lee`; `ann.lee-2` if taken), or takes over a user an admin made beforehand with the same verified email. Roles are decided then and never changed by a later sign-in, so a provider's claims changing cannot silently promote anyone; change them with `odcl users set` or on the Account page.
- **API tokens** of single-sign-on users stop working after `ODCL_OIDC_TOKEN_IDLE_DAYS` without signing in, so people removed from the provider lose API access too. Signing in again brings them back. Service users made with `odcl users add` keep their tokens.
- **A browser session changes nothing from another site**: a write made with the session cookie must come from the server's own page (its `Origin`).
- **Signing out** (on the Account page) ends the session, and the provider's too where it offers that (`end_session_endpoint`).

## Documents

A team's knowledge-base pages, findings and runbooks can say which data they describe, so a table or column shows the pages about it. A page says so in its frontmatter, and its body stays as it is:

```markdown
---
title: "Order status codes"
owner: Data Team
odcl:
  kind: reference                # reference, finding, runbook, glossary: free text, short
  describes:
    - key: column:warehouse.sales.orders.status_code
      section: status-codes      # optional: a heading anchor in this page
      says: "Closed = status_code IN ('C', 'X')"    # optional, one line
    - key: table:warehouse.ref.status_lookup
    - pattern: "column:*-prod.sales.orders.amount*" # a glob over entry keys
---
# Order status codes
…
```

```sh
uv sync --extra docs
uv run odcl import --docs ../knowledge-base/docs
uv run odcl docs warehouse.sales.orders.status_code
```

- **When another tool owns the frontmatter**, such as a linter that allows only its own keys, put the block in an HTML comment anywhere in the page instead (outside code blocks: one shown as an example in a fence is left alone). It holds what the `odcl:` key would, and is left out of the page's text:

  ```markdown
  <!-- odcl
  kind: finding
  describes:
    - key: column:warehouse.sales.orders.amount
  -->
  ```
- **Importing.** `import --docs` reads every `*.md` file with an `odcl:` block (or `<!-- odcl -->` comment) under the folders it is given and counts the others as skipped. A page becomes a `document` entry keyed `document:<repo>/<path>`, holding its title, owner, kind, text, headings with their anchors, commit, web address and content digest.
- **Commit and web address.** They come from git when the page is in a checkout, else from `--commit` and `--repo-url`.
- **Revisions.** Importing an unchanged page changes nothing; an edited one is a new revision.
- **Search** covers the title, headings and text.
- **`describes` links.** Each one pins the revision of the table, column or project it names. Re-importing an unchanged page keeps that pin, because nobody re-read the page. So when the column is redefined later, its page, the column and `odcl overview` all say it was redefined after the page described it, until the page is edited.
- **A column** also shows the pages that describe its table.
- **Patterns** are resolved at every import, one link per entry they match, and kept on the page, so a later import links copies added since.
- **What is not found.** A key or pattern that matches nothing, or a section the page has no heading for, is not an error. It is reported by the import and on the page.
- **Retirement.** A page imported before and now gone from the folder, or that no longer has an `odcl:` block, is retired: a new revision that describes nothing. Its links stay in its history, not as current. Importing one file never retires the others.
- **One transaction.** Every file of one import is written together.

The web page has a page for each document, with its links grouped by section and its text shown as text; nothing in a page runs. Every entry has a "Documented in" panel, and search finds documents. The MCP server has a `documents_for` tool, and `find_data` returns documents. `Catalog.put("document", …)` and `odcl.docs.import_docs` are the Python interface.

**A model version can rely on a document**, such as a finding about how a field behaves. The training manifest lists it under `relied_on` (a document key, or `{"document", "digest" or "revision", "why"}`), or `odcl rely MODEL --version V DOCUMENT… --why TEXT` records it afterwards. Each is pinned at one revision: the one the manifest names or whose content digest it gives, else the one current when the training task ended. When the page is corrected later, `odcl explain model`, the model and document pages and `odcl overview` say which model versions used the old text.

## What created a table or file

Some data is made by a script or a notebook rather than a pipeline the catalog reads. A table (which in odcl also stands for a view or a file) can say what created it: its kind (`script`, `notebook`, `query`, `job`, `manual` or `other`), path, repository, commit, link, the command that ran it and a note.

```sh
uv run odcl created-by lake.cohorts.monthly notebooks/build_cohort.ipynb --note "run by hand each month"
uv run odcl created-by warehouse.ref.holidays make_holidays.py --command "python make_holidays.py --year 2027"
uv run odcl record dataset-files --uri gs://bucket/cohort/2026-10 --created-by notebooks/export.ipynb
```

Given a local file, the repository, the path in it, the commit that last changed it and a link come from git, and the kind from its extension (`.ipynb` is a notebook). A file with uncommitted changes is marked so, because that commit is not the code that ran. A link or a path in `--repo-url` works too. It is what a person said, not something odcl checked: the web page and `odcl explain` show it "as stated". For files a pipeline task did not write, `record dataset-files --created-by` fills the "written by" a URI alone leaves unknown. Imports that merge keep it; `--clear` forgets it.

## Foreign keys

A column can say which column its values are drawn from: a `references` link, pinned like every other link. It is not lineage, so it changes neither what a column is built from nor who uses it. A column lists what it references and what references it, and a table lists its foreign keys and the columns of other tables that reference it.

```sh
uv run odcl references shop.public.orders.customer_id shop.public.customers.id     # no target clears them
uv run odcl add column shop.public.orders customer_id --references shop.public.customers.id
```

Importers fill them in: BigQuery's table constraints and Postgres foreign keys. A key to a table that is neither registered nor in the same import is left out and named in the import's result: `unresolved_references` for BigQuery, `skipped` for Postgres.

## Copies of one definition

One definition is often deployed many times: the same table in every tenant's project, say. A definition table can name its copies by glob patterns over their paths, and the catalog links each match with an `instance_of` link:

```sh
uv run odcl instances core.sales.daily_totals 't-*.sales.daily_totals'
```

- **Lineage goes through copies.** A copy is read as built from its definition, and each of its columns from the definition's column of the same name. So the lineage of a model's input runs from the tenant copy it read to the definition's SQL and on to what that reads, and the definition's columns show the projects that use any copy.
- **Patterns** are kept on the definition and resolved whenever it is written. A table registered later that matches is linked when it is registered; an import links all the copies it registered at its end, in one new revision of the definition.
- **Shown** on both: a definition lists its patterns and copies, and a copy names its definition. `Catalog.set_instances` is the Python interface; `odcl instances DEFINITION` with no patterns clears them.

## Privacy

Tables and columns can say whether they hold personal data (`pii`), how it is scrubbed (`scrubbing`: hashed, truncated, removed…), their data category (`data_category`) and how long they are kept (`retention`). A column that names no category or retention of its own takes its table's. Only what is said is kept: a column that says nothing about `pii` is unknown, not "no".

```sh
uv run odcl privacy set warehouse.crm.customers --data-category contact --retention 400d
uv run odcl privacy set warehouse.crm.customers.email --pii yes --scrubbing hashed
uv run odcl privacy export --pii-only > privacy.csv        # or --json; --table for one table
```

`odcl add table|column` take the same flags. A dbt project can say them in `meta` (directly, or under `meta.privacy`) on a source, a model or a column. Imports that merge (BigQuery, Dataform, SQL definitions, OpenLineage) keep what was set; a dbt import says everything about its models, so for those keep the fields in dbt's `meta`. The web page shows them on each entry, and `GET /v1/privacy` answers what `privacy export` does.

## For agents

```sh
uv run odcl mcp
```

serves `find_data`, `get_entry`, `trace_lineage`, `documents_for`, `used_by`, `what_changed` and `project_uses` over the Model Context Protocol (the official [MCP SDK](https://github.com/modelcontextprotocol/python-sdk)). With `--allow-writes` it also offers `register_project` and `record_use`, to a direct connection or, with `$ODCL_TOKEN`, to an editor or above.

```sh
ODCL_TOKEN=odcl_... uv run odcl mcp --server https://catalog.example.com --allow-writes
```

uses an `odcl serve` instead of the database, so an assistant needs only the person's API token, and what it writes is recorded as that person. The address is `https://` (or `http://` on this machine), so the token is never sent in the clear.

## From Python

```python
from odcl import Catalog, migrate

migrate()  # reads ODCL_DATABASE_URL
with Catalog() as catalog:
    catalog.find("plan", layer="gold")
    catalog.used_by("churn.bronze.accounts.plan")
```

`Catalog` is the whole interface: `put`, `apply`, `use`, `find`, `get`, `lineage`, `used_by`, `uses`, `table_history`, `query_jobs`, `tables`, `projects`, `runs`, `models`, `overview`. `apply` registers a batch in one transaction: all of it, or none; with `withdraw` it also withdraws what that importer said before within what it read and no longer saw (`Catalog.withdraw`). It takes every entry lock first (512 stripes that keys hash to, in order), so an import of any size fits PostgreSQL's lock table and two imports run one after the other rather than deadlock. With `merge=True`, fields a payload leaves out keep what the entry already says, and the tags a payload sends belong to the one importer named in its `source`, so they replace only that importer's earlier tags. Without `merge` a payload says the whole entry, except what is said beside it by its own commands: copies (`instances`), `created_by`, a column's `references` and the privacy fields are kept when the payload leaves them out, and cleared when it sets them to null. So a dbt import, which does not merge, keeps them. Builds are read and written through `odcl.dataform.import_invocation`, `odcl.builds.record_handoff` and `odcl.builds.explain_dataset`; model versions through `odcl.vertex.import_build` and `Catalog.explain_model`; OpenLineage events through `odcl.openlineage.ingest`. `odcl.server.make_server` returns the HTTP server. `Catalog(token=...)` signs in as an API token's user and checks their role on every write; `odcl.users` manages users and tokens.

## Developing

Tests run against a real PostgreSQL database. Each test creates its own schema in it and drops it afterwards.

```sh
createdb odcl_test
ODCL_TEST_DATABASE_URL=postgresql://localhost/odcl_test uv run pytest -q
```

Without `ODCL_TEST_DATABASE_URL` the database tests are skipped locally and fail in CI. Schema changes go in a new numbered file in `src/odcl/migrations/`.

**The docs site**, https://lawwu.github.io/open-data-context-layer/, is drawn from this README, [`CHANGELOG.md`](CHANGELOG.md) and the pages in `docs/` with [Zensical](https://zensical.org) ([`zensical.toml`](zensical.toml)), so there is no second copy to keep in step. [`scripts/docs.py`](scripts/docs.py) assembles those files into `build/docs/` first, pointing links written for GitHub at the site's pages, or at the file on GitHub when the site has none. Every push to `main` publishes it (`.github/workflows/pages.yml`); pull requests build it in strict mode, so a broken link or anchor fails there. A new page in `docs/` goes in `nav` in `zensical.toml`.

```sh
uv run --only-group site python scripts/docs.py serve   # then http://127.0.0.1:8000/; restart after editing
uv run --only-group site python scripts/docs.py build   # strict, into site/
```

**The workflows** (`.github/workflows/`): `ci.yml` runs ruff and the tests on PostgreSQL 16 and merges Dependabot's minor and patch updates once they pass; `pages.yml` builds and publishes the docs; `release.yml` and `latest.yml` cut releases (below); `actions.yml` checks the workflows themselves. They name exact action versions, and `.github/workflows/actions.lock` records each one's commit, so a release tag moved to other code later can't change what runs. After changing a workflow, relock with GitHub's [gh-actions-lock](https://github.com/github/gh-actions-lock) and check it with [zizmor](https://docs.zizmor.sh), as CI does:

```sh
gh extension install github/gh-actions-lock   # once
gh actions-lock                               # pin new or changed actions
uvx zizmor .github/                           # security lint for the workflows
```

Relocking rewrites each `uses:` line it pins and drops a comment at the end of that line, so keep comments (zizmor's `# zizmor: ignore[...]` among them) on lines of their own. Dependabot's weekly update of the actions updates the lock file too.

## Versioning and releases

Releases are git tags, `vX.Y.Z`, following semantic versioning; [`CHANGELOG.md`](CHANGELOG.md) says what each one changed. odcl is not on PyPI (the `bigquery` extra depends on Sounding by git URL, which PyPI refuses), so install a release from GitHub, a specific one with `@vX.Y.Z` or the newest with `@latest`:

```sh
uv tool install "open-data-context-layer[mcp] @ git+https://github.com/lawwu/open-data-context-layer@latest"
```

Without the `@…` you get the latest `main`, which takes Dependabot's updates as soon as they merge, before any release. Each release on GitHub also carries the sdist and wheel, with [build provenance](https://docs.github.com/actions/security-for-github-actions/using-artifact-attestations) (`gh attestation verify FILE -R lawwu/open-data-context-layer`).

To cut a release:

1. On a branch, move the changelog's "Unreleased" notes under a new `## X.Y.Z (YYYY-MM-DD)` heading and leave "Unreleased" empty.
2. Bump the version, which updates `pyproject.toml` and `uv.lock`, and set the same version in `src/odcl/__init__.py` (CI fails while the two disagree):

   ```sh
   uv version --bump minor   # or patch / major
   ```

3. Open a pull request titled `Release X.Y.Z: …` and merge it once CI passes.
4. Tag the merge commit on `main` and push the tag:

   ```sh
   git switch main && git pull
   git tag -a vX.Y.Z -m "X.Y.Z"
   git push origin vX.Y.Z
   ```

The tag starts `release.yml`, which refuses a tag that isn't on `main`, whose version doesn't match `pyproject.toml` and `src/odcl/__init__.py`, or that has no changelog section. It builds the sdist and wheel, checks the wheel installs and runs, attests both, and creates the GitHub release with the changelog section as its notes (a release already made by hand keeps its title and notes and gets the files). `latest.yml` moves the `latest` tag to the highest `vX.Y.Z`, so a patch to an older line doesn't move it back. Both can also be run by hand from the Actions tab.

## Not built yet

- The machine-learning shape on top: training and serving features, labels, training datasets and model cards.
- Proposing manifests for pipelines laid out differently from a trainer with a Model output and a `metadata.json` beside it.
- Sending the catalog's runs and lineage out as OpenLineage events.
- Discovering Dataform invocations, and Dataform runs started from the CLI rather than the managed service.
- Permissions finer than a role, such as who may change which tables. Whoever holds the database's credentials can still do anything.
- The CLI through `odcl serve` (as `odcl mcp --server` does), so people annotate from the command line without database credentials.
- Retiring what an OpenLineage producer stopped emitting: events say what a run touched, not everything that exists.

The distribution is `open-data-context-layer`, the Python package is `odcl`, and the command is `odcl`. The repository is [lawwu/open-data-context-layer](https://github.com/lawwu/open-data-context-layer).
