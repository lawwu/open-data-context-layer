# Open Data Context Layer

A data context layer: what data exists, how it connects, and which projects use it. It is for people and for AI agents, so both can find the right data and see what a change would affect.

The catalog holds descriptions. It does not store or serve the data itself. Everything it keeps lives in one PostgreSQL database.

The project was called Fathom. The [implementation plan](docs/implementation-plan.md) describes where it is going. Its first goal, a model trace from a Vertex AI Pipelines training run back through Dataform and BigQuery to the raw columns, is built and has been run against a real project ([`examples/live_trace/`](examples/live_trace/)).

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
| `odcl find WORDS [--kind] [--layer]` | What data is there for this? |
| `odcl show KEY` | Everything about one entry, with its history |
| `odcl lineage KEY` | What is it built from, and what is built from it? |
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
| `odcl import-sqlite PATH` | Bring an old Fathom SQLite file across, once |
| `odcl import --openlineage FILE` | Read OpenLineage events: runs and the lineage they establish |
| `odcl import --docs PATH... [--repo-url URL] [--commit SHA]` | Read Markdown pages that describe catalog entries |
| `odcl docs KEY` | Which documents describe this entry? |
| `odcl serve [--accept-lineage]` | Serve the catalog over HTTP, with a web page to browse it |
| `odcl mcp [--allow-writes]` | Serve all of this to an assistant |

Add `--json` before the command for the full result. A bare path such as `churn.bronze.accounts` works wherever a key does, when only one entry has it.

The database is one PostgreSQL database: `--database-url`, else `$ODCL_DATABASE_URL`. Run `odcl migrate` once on a new database and again after upgrading. Changes are recorded under `--as`, else `$ODCL_USER`, else your login name. There is no sign-in.

The `fathom` command still works as an alias for `odcl`, for now.

## Coming from Fathom on SQLite

```sh
uv run odcl migrate
uv run odcl import-sqlite ~/.fathom/fathom.db
```

This copies every entry, every revision with its ID, author and time, and every link into an empty database, in one transaction. The SQLite file is only read. The old file recorded links for each entry's latest revision only; the import rebuilds the links of older revisions from what those revisions stored, so `odcl uses PROJECT --revision REV` can answer what a project had pinned earlier. If the file does not hold together, for example a link pins a revision that is missing, nothing is imported and the message names the link.

Python code that imports `fathom_data` keeps working: `Fathom` is now `odcl.Catalog`, and it takes a database URL where it took a file path.

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

There is no sign-in: anyone who can reach the server can read the whole catalog. That is why it listens on 127.0.0.1 unless `--host` says otherwise. It writes nothing unless started with `--accept-lineage`. With `ODCL_LINEAGE_TOKEN` set, a sender must also present that token as `Authorization: Bearer TOKEN`. The page runs only its own script, under a content security policy, and displays catalog text as text.

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

serves `find_data`, `get_entry`, `trace_lineage`, `documents_for`, `used_by` and `project_uses` over the Model Context Protocol (the official [MCP SDK](https://github.com/modelcontextprotocol/python-sdk)). With `--allow-writes` it also offers `register_project` and `record_use`.

## From Python

```python
from odcl import Catalog, migrate

migrate()  # reads ODCL_DATABASE_URL
with Catalog() as catalog:
    catalog.find("plan", layer="gold")
    catalog.used_by("churn.bronze.accounts.plan")
```

`Catalog` is the whole interface: `put`, `apply`, `use`, `find`, `get`, `lineage`, `used_by`, `uses`, `table_history`, `query_jobs`, `tables`, `projects`, `runs`, `models`, `overview`. `apply` registers a batch in one transaction: all of it, or none. It takes every entry lock first (512 stripes that keys hash to, in order), so an import of any size fits PostgreSQL's lock table and two imports run one after the other rather than deadlock. With `merge=True`, fields a payload leaves out keep what the entry already says, and the tags a payload sends belong to the one importer named in its `source`, so they replace only that importer's earlier tags. Without `merge` a payload says the whole entry, except what is said beside it by its own commands: copies (`instances`), `created_by`, a column's `references` and the privacy fields are kept when the payload leaves them out, and cleared when it sets them to null. So a dbt import, which does not merge, keeps them. Builds are read and written through `odcl.dataform.import_invocation`, `odcl.builds.record_handoff` and `odcl.builds.explain_dataset`; model versions through `odcl.vertex.import_build` and `Catalog.explain_model`; OpenLineage events through `odcl.openlineage.ingest`. `odcl.server.make_server` returns the HTTP server.

## Developing

Tests run against a real PostgreSQL database. Each test creates its own schema in it and drops it afterwards.

```sh
createdb odcl_test
ODCL_TEST_DATABASE_URL=postgresql://localhost/odcl_test uv run pytest -q
```

Without `ODCL_TEST_DATABASE_URL` the database tests are skipped locally and fail in CI. Schema changes go in a new numbered file in `src/odcl/migrations/`.

## Not built yet

- The machine-learning shape on top: training and serving features, labels, training datasets and model cards.
- Proposing manifests for pipelines laid out differently from a trainer with a Model output and a `metadata.json` beside it.
- Sending the catalog's runs and lineage out as OpenLineage events.
- Discovering Dataform invocations, and Dataform runs started from the CLI rather than the managed service.
- Sign-in and permissions. Today anyone who can reach the database can read and write the catalog.

The distribution is `open-data-context-layer`, the Python package is `odcl`, and the command is `odcl`. The repository is [lawwu/open-data-context-layer](https://github.com/lawwu/open-data-context-layer); it was `lawwu/fathom`, and GitHub redirects the old address.
