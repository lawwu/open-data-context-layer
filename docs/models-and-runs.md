# Models and runs

Tracing a model version back through the build that made its training data to the raw columns.

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

The training task writes a manifest after it uploads the model: which pipeline run and task it is, the dataset version it read, the fields of the training matrix in order with each one's role (`feature`, `label`, `cohort_filter`, `join_key`, `weight`), and the artifact's Cloud Storage URI, object generation and SHA-256. [`odcl.manifest`](../src/odcl/manifest.py) writes one and has no dependencies, so training code can vendor that file. Take the fields from the matrix as fitted, not from configuration.

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
