# Importing

Where the catalog's tables, columns and lineage come from, and what each import records.

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

### What is used most

From the synced job history, odcl counts how many query jobs read each table (a script and its statements count once) and through how many query shapes:

```sh
uv run odcl popular --since 30d            # the most read tables, registered or not
```

- The tables list has a `reads` column (last 30 days) and sorts by it (`sort=reads` over HTTP, "most read first" on the web page).
- Search lifts a match on a much-read table, or a column of one, a little: by `1 + 0.1·ln(1 + reads)`, enough to order close matches and not to put a poor match on top.
- A table's page and `get` have `usage`: its reads, query shapes and last read, and the tables most often read by the same jobs ("often read with"), which is how people find what to join it to.
- The job history does not keep who ran a query, so neither does this. `GET /v1/popular?since=&limit=`.

## Importing from PostgreSQL

odcl reads a PostgreSQL database's own catalog: its tables (ordinary, partitioned and foreign), views and materialized views, their columns, and their foreign keys. It reads no rows.

```sh
uv run odcl import --postgres postgresql://reader@db.internal/shop --schema public sales
```

Keep the password out of the URL, where it would land in shell history: set `PGPASSWORD` or use a `~/.pgpass` file, which libpq reads.

- **Keys** are `database.schema.table` (`--database-name` to key them by another name), lower-cased, like every key. The URL, and any password in it, is not kept.
- **Descriptions** are the tables' and columns' comments; each column keeps its type, position and whether it may be null.
- **View lineage.** Each view's SQL is read with every table it reads named by its schema, and each of its columns is traced through it as a Dataform definition is, in PostgreSQL's dialect: through CTEs and `SELECT *`, from views to the tables under them, and tagged `lineage:partial` when only partly traced. Quoted mixed-case names match their lower-cased keys.
- **Foreign keys** become `references` links. A partitioned table's key is read once, not again for each partition. A key to a schema not read is named in `unresolved`. Named in `skipped`, with why: a name the catalog cannot key (and any key on such a column), and a second table or column whose name differs from another's only in case.
- **Tables in schemas not read**, that a view reads, are registered by name and named in `unresolved` on every import until something registers them. The column names a view's SQL gives them are not taken as their full list, so importing again traces the view the same way.
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

## What every import answers

Every import of tables and columns (dbt, BigQuery, PostgreSQL, Dataform, SQL definitions) answers the same seven fields, and says its own beside them:

| Field | What it holds |
| --- | --- |
| `added`, `changed`, `unchanged` | counts of entries |
| `retired`, `withdrawn` | keys of what is gone from the source (next section) |
| `unresolved` | what the source named that the catalog does not have: a table registered by name only, or a link not made, written `from -> to` |
| `skipped` | what was read and could not be registered, each with why |

Importing the same thing again changes nothing, and an import registers all of what it read, or none.

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

Importers fill them in: BigQuery's table constraints and Postgres foreign keys. A key to a table that is neither registered nor in the same import is left out and named in the import's result as `unresolved`, for BigQuery and Postgres.

## Copies of one definition

One definition is often deployed many times: the same table in every tenant's project, say. A definition table can name its copies by glob patterns over their paths, and the catalog links each match with an `instance_of` link:

```sh
uv run odcl instances core.sales.daily_totals 't-*.sales.daily_totals'
```

- **Lineage goes through copies.** A copy is read as built from its definition, and each of its columns from the definition's column of the same name. So the lineage of a model's input runs from the tenant copy it read to the definition's SQL and on to what that reads, and the definition's columns show the projects that use any copy.
- **Patterns** are kept on the definition and resolved whenever it is written. A table registered later that matches is linked when it is registered; an import links all the copies it registered at its end, in one new revision of the definition.
- **Shown** on both: a definition lists its patterns and copies, and a copy names its definition. `Catalog.set_instances` is the Python interface; `odcl instances DEFINITION` with no patterns clears them.
