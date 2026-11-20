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

You need a PostgreSQL database the catalog may create its tables in.

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
| `odcl sync bigquery-jobs --project PROJECT` | Read the query jobs a project ran since the last sync |
| `odcl history table KEY [--since 7d] [--patterns]` | Which query jobs read or wrote this table? |
| `odcl import --dataform INVOCATION` | Read what a Dataform run compiled and how each action ran |
| `odcl handoff --invocation INVOCATION --target TABLE --snapshot SNAPSHOT` | Record that a build handed on a snapshot of its output |
| `odcl explain dataset SNAPSHOT[@TIME]` | Which build, job, file and inputs a dataset version came from |
| `odcl record dataset-files --pipeline-run JOB --task TASK --output NAME [--from-table TABLE] [--export-job P:L.JOB \| --find-export-job]` | Record the files a pipeline task exported as a dataset version, bound to the BigQuery job that wrote them |
| `odcl import --vertex-manifest PATH\|gs://...` | Check a training manifest against Vertex AI and Cloud Storage, and record the model version |
| `odcl explain model NAME --version V` | Which run, artifact, dataset version and raw columns a model version came from |
| `odcl overview` | Counts, and data that changed after a project used it |
| `odcl migrate` | Create or update the catalog's tables |
| `odcl import-sqlite PATH` | Bring an old Fathom SQLite file across, once |
| `odcl import --openlineage FILE` | Read OpenLineage events: runs and the lineage they establish |
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

The BigQuery commands need the `bigquery` extra and Python 3.14 or later, because they read through [Sounding](https://github.com/lawwu/sounding). Sign in once with `gcloud auth application-default login`. Every call is free: `tables.get` for schemas and `jobs.list` for jobs. No query is run.

```sh
uv sync --all-extras --python 3.14
uv run odcl import --bigquery my-project.features.shift_inputs my-project.raw.shift_totals
uv run odcl sync bigquery-jobs --project my-job-project
uv run odcl history table my-project.features.shift_inputs --since 7d --patterns
```

**Schemas.** Each table becomes a table entry and each field a column, nested fields by dotted path (`shift.hours_at_cutoff`). A snapshot records its base table and snapshot time. Row counts and sizes are left out, so importing again changes nothing unless the definition changed.

**Job history.** `sync bigquery-jobs` reads the query jobs created in one project, the project the jobs ran in, which may differ from where the tables live. Reading other people's jobs needs `bigquery.jobs.listAll` on that project.

- The first sync goes back 7 days (`--backfill`, up to the 180 days BigQuery keeps). Each later sync starts 6 hours before the last finished window ended, or at the oldest job still running, and keeps every job once.
- A sync that is interrupted keeps the pages it read but does not record the window as finished, so the next sync reads it again.
- A job that finished is never replaced by an older reading that says it is still running.
- Jobs labelled as a metadata collector's own (`sounding_action`, `odcl_action`) are left out.

**What a job tells you.** A `read` is a table the job reported referencing. A `write` is recorded only for a statement that succeeded and named a lasting destination; a query's temporary result table is not one. Cache hits and failed jobs often report no tables, and are kept with a `no_table_references` gap. A script and its statements are both kept; totals count the statements, not the script as well.

A job shows that a table was queried. It does not show which fields a model was trained on.

**SQL.** By default each query is kept only as its shape: comments removed and every literal and parameter replaced by `?`, so two lookups for different employees are one example. The original SQL is not stored. A script, or SQL that cannot be read for certain, is kept without a shape (`template_unavailable`). `--sql none` does not ask BigQuery for SQL at all. Error messages and user emails are never read.

Grouping uses the catalog's own digest of the shape. BigQuery's normalized query hash is only in `INFORMATION_SCHEMA`, which costs a query to read.

## Importing from Dataform

```sh
uv sync --extra dataform
uv run odcl import --dataform projects/P/locations/L/repositories/R/workflowInvocations/I
```

This reads one workflow invocation and the compilation result it ran, with free Dataform calls. A definition and a run are kept apart:

- **Definitions.** Every table the compilation declares or builds becomes a table entry, with its file and the tables it reads. Every column the compiled query selects becomes a column entry with its expression and the columns it is computed from, traced by [sqlglot](https://github.com/tobymao/sqlglot). A column it cannot trace fully is tagged `lineage:partial`; SQL it cannot read marks the table's columns partial and drops their old definitions.
- **Runs.** The invocation and each action are recorded as executions: state, times, and the BigQuery job ID. The job itself is looked up in synced query history when a trace is read, so `odcl sync bigquery-jobs` may run before or after the import.
- **Shared entries.** BigQuery and Dataform describe the same physical tables and columns, so they share keys. Each import adds what it knows (BigQuery the types, Dataform the expressions) and keeps what the other said. Each keeps its own details under its name in `source`.
- **Order.** An invocation's definitions are applied only if its compilation is at least as new as the newest already imported from that repository. An older invocation imported late still has its runs recorded, without a pinned definition, and its trace says so.

Import an invocation by name for now. Discovering invocations is not built yet.

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

## For agents

```sh
uv run odcl mcp
```

serves `find_data`, `get_entry`, `trace_lineage`, `used_by` and `project_uses` over the Model Context Protocol (the official [MCP SDK](https://github.com/modelcontextprotocol/python-sdk)). With `--allow-writes` it also offers `register_project` and `record_use`.

## From Python

```python
from odcl import Catalog, migrate

migrate()  # reads ODCL_DATABASE_URL
with Catalog() as catalog:
    catalog.find("plan", layer="gold")
    catalog.used_by("churn.bronze.accounts.plan")
```

`Catalog` is the whole interface: `put`, `apply`, `use`, `find`, `get`, `lineage`, `used_by`, `uses`, `table_history`, `query_jobs`, `tables`, `projects`, `runs`, `models`, `overview`. `apply` registers a batch in one transaction: all of it, or none. With `merge=True`, fields a payload leaves out keep what the entry already says. Builds are read and written through `odcl.dataform.import_invocation`, `odcl.builds.record_handoff` and `odcl.builds.explain_dataset`; model versions through `odcl.vertex.import_build` and `Catalog.explain_model`; OpenLineage events through `odcl.openlineage.ingest`. `odcl.server.make_server` returns the HTTP server.

## Developing

Tests run against a real PostgreSQL database. Each test creates its own schema in it and drops it afterwards.

```sh
createdb odcl_test
ODCL_TEST_DATABASE_URL=postgresql://localhost/odcl_test uv run pytest -q
```

Without `ODCL_TEST_DATABASE_URL` the database tests are skipped locally and fail in CI. Schema changes go in a new numbered file in `src/odcl/migrations/`.

## Not built yet

- The machine-learning shape on top: training and serving features, labels, training datasets and model cards.
- Discovering Vertex AI pipeline runs; a model version is imported from its manifest for now.
- Sending the catalog's runs and lineage out as OpenLineage events.
- Discovering Dataform invocations, and Dataform runs started from the CLI rather than the managed service.
- Sign-in and permissions. Today anyone who can reach the database can read and write the catalog.

The distribution is `open-data-context-layer`, the Python package is `odcl`, and the command is `odcl`. The repository is [lawwu/open-data-context-layer](https://github.com/lawwu/open-data-context-layer); it was `lawwu/fathom`, and GitHub redirects the old address.
