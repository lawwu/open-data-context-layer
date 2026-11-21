# Open Data Context Layer implementation plan

Build a catalog for a data science team using BigQuery, Google Cloud Dataform and Vertex AI Pipelines. Use **one PostgreSQL database** for every piece of catalog state, including revisions, query history, ingestion checkpoints, execution records, document text, lineage and search. BigQuery holds the source data and Vertex continues to use its existing artifact storage. A model registry is optional.

The first deliverable is one command: `odcl explain model staffing-forecast --version v17`. A scientist starting with that model version should find the exact training task, the dataset version it read, the selected feature, the Dataform action and BigQuery job that produced it, and the raw column behind it. The identifiers below are illustrative. All new commands and interfaces in this document are proposed.

## The first model trace

Use one raw BigQuery table, one Dataform table action with a simple GoogleSQL expression, and one Vertex training component. Have the training task read a frozen copy of the feature table. Make the first fixture small enough for a scientist to verify the whole chain by hand.

```text
staffing-forecast / v17
  produced by   Vertex pipeline run train-042 / task train / execution 481
  artifact      gs://demo-models/staffing/v17/model.bin, with generation and SHA-256
  read          workforce-demo.frozen.shift_inputs_b042, snapshot of features.shift_inputs
  selected      hours_at_cutoff as a feature
  produced by   Dataform invocation build-042 / target features.shift_inputs
  executed as   BigQuery job (workforce-demo, us-central1, job-042)
  defined in    definitions/shift_inputs.sqlx at commit 91fa…
  derived from  workforce-demo.raw.shift_totals.worked_minutes_asof / 60
```

The **feature table**, its **column definition**, and the **data read by the run** have different identities. A new description can change a catalog revision. A new pay period can change the rows while the definition stays identical. An immutable snapshot or write-once input identifies the training data. This first trace establishes the raw *column definition* behind the feature; reproducing the underlying raw rows would also require retained versions of those inputs. [BigQuery table snapshots](https://docs.cloud.google.com/bigquery/docs/table-snapshots-intro)

## Postgres data model

Keep the current distinction between a stable catalog entry and an immutable revision. Use relational columns for identities and joins, `timestamptz` for times, and `jsonb` for source-specific attributes. The table names below are proposed migration targets. Rows for documents and cards can follow the first trace.

| Table | Keys and contents |
| --- | --- |
| `entries` | Stable `key` (PK), `kind`, `head_revision`. Start with table, column, project, Dataform action and model version. Add document in the cards milestone. A project may represent a model family. Index the current searchable text with a Postgres `tsvector` and GIN index. |
| `revisions` | `id` (PK), `entry_key` (FK), canonical body, digest, actor, recorded time. Constrain the head to a revision of the same entry. Compare the digest with the head under a row lock; a later return to an older value is a new historical event. |
| `revision_links` | `source_revision`, relation, `target_key`, `target_revision`, optional purpose. Enforce that the pinned revision belongs to the target. Preserve existing `derived_from` and project `uses`. Use links such as action `depends_on` action, document `describes` asset and model version `relied_on` finding. Index both directions. A current-links view joins through source heads. |
| `executions` | PK `id`; unique `(system, native_execution_key)`. Kinds: Dataform action execution, Vertex pipeline run, Vertex task execution. Store parent execution where appropriate, definition revision, state and times. Dataform evidence includes invocation, resolved compilation result, effective target and BigQuery job coordinates. Vertex evidence includes the full PipelineJob resource name and task execution/attempt identity. |
| `query_jobs` | Composite PK `(execution_project, location, job_id)`. Store parent job ID, creation/start/end times, state, statement type, error reason, cache hit, processed/billed bytes, slot milliseconds and normalized query hash when available. Index by creation time and parent identity. Join a Dataform execution using these exact job coordinates. The job may arrive before or after the invocation. Ad hoc jobs stand on their own. |
| `query_tables`, `query_patterns`, `sync_cursors` | For each job store distinct `(job key, physical table key, role)` observations such as referenced table or write target, with the evidence source. Index `(table key, role)` for reverse lookup. A job can point to a parameterized GoogleSQL template in `query_patterns`, unique by scope and template digest. Store one completed ingestion window per source, execution project and location in `sync_cursors`. |
| `dataset_versions` | PK `id`; unique immutable BigQuery reference including creation/snapshot identity. FK to the logical table revision and producing Dataform execution. Record snapshot base and time, plus how the producer-to-snapshot handoff was verified. Many data versions may share one logical table definition. |
| `version_columns` and `field_inputs` | Each version column has key `(dataset_version_id, field_path)`, a column revision FK and a lineage status. Each field input references that composite key and a pinned upstream column revision, with compiled-expression evidence and method. The first trace needs one hop to a raw column. Record unresolved references as diagnostics and return partial coverage when the SQL cannot be resolved. |
| `model_builds` | One row per model-version entry. Unique `(project_key, version)`, immutable model revision FK, producing Vertex task execution FK, artifact URI/generation/digest, training code commit, cutoff and manifest digest. That model revision pins the finding revisions relied on at training. A second artifact claiming the same model version is a conflict. Keep authored model-card text in its own revision history. |
| `build_inputs` | Build FK, role, field name as used by the training code, optional feature order, and composite FK to `version_columns`. Roles include feature, label, cohort filter and join key. A field can be a feature for one model and a label for another. |

**Identity across imports.** Construct one BigQuery identity from the effective project, dataset, table and field path. Dataform's physical target and Vertex's BigQuery reference resolve to that identity. Preserve native spelling and nested path boundaries. The existing resolver lowercases bare paths; keep compatible aliases for existing keys and report ambiguous matches. Key a Dataform action definition by repository and canonical target, and its execution by invocation and action. Keep development target overrides separate from production. Keep legacy revision IDs when importing old catalogs.

**Historical reads.** Follow `model_builds → build_inputs → version_columns → field_inputs` for the field path, and the two execution FKs for Vertex and Dataform evidence. Resolve pinned revisions throughout. Use current heads only when explicitly asking what changed since training. A later source scan must leave the answer for v17 intact. Use recursive queries for catalog lineage and indexed reverse joins for impact analysis. [Postgres recursive queries](https://www.postgresql.org/docs/current/queries-with.html) · [Postgres full-text indexes](https://www.postgresql.org/docs/current/textsearch-indexes.html)

## Evidence to collect

1. **BigQuery supplies physical identities and schemas.** Fetch metadata for the fixture's raw table, feature table and snapshot. Register nested fields, types and descriptions. Capture the schema observed for the build. When backfilling an older run, mark historical schema evidence unavailable if only today's schema can be retrieved. [BigQuery table metadata](https://docs.cloud.google.com/bigquery/docs/reference/rest/v2/tables/get)
2. **Dataform supplies definitions and executions separately.** Read the invocation's `resolvedCompilationResult`, page through its compiled actions, and record the canonical/effective targets, SQLX path, dependency targets, compiled query and resolved Git SHA when present. Parse the compiled query using the BigQuery dialect and the input schemas. Read the invocation actions for execution state and job ID. A declared assertion dependency is an ordering relationship; a parsed value expression is column lineage. A failed or skipped action remains visible as an execution. [Compilation actions](https://docs.cloud.google.com/dataform/reference/rest/v1/projects.locations.repositories.compilationResults/query) · [Compilation source revision](https://docs.cloud.google.com/dataform/reference/rest/v1/projects.locations.repositories.compilationResults) · [Invocation details](https://docs.cloud.google.com/dataform/reference/rest/v1/projects.locations.repositories.workflowInvocations) · [Invocation actions](https://docs.cloud.google.com/dataform/reference/rest/v1/projects.locations.repositories.workflowInvocations/query)
3. **Bind the actual data to the producer.** For the pilot, serialize writes to one feature table, have its successful build handoff create a uniquely named snapshot before the next write, and record the invocation, action, job and snapshot identity together. The training component receives and reads that snapshot. Validate the snapshot's base and time against the handoff. An alternative for a pipeline already producing write-once tables is to pass that output identity directly. A table name and the most recent successful job are insufficient to select the producer. Report `producer_unknown` when evidence is missing.
4. **Vertex and the training component supply the consumer evidence.** Import a named PipelineJob, the task execution that performed training, status and available artifact references. Have the training code emit a small manifest after writing the artifact. Build the selected fields from the actual training matrix/config at fit time. Record document revision IDs at decision time if findings influenced the model. A task that only passes a table name as a string needs this instrumentation. [Vertex pipeline artifact lineage](https://docs.cloud.google.com/vertex-ai/docs/pipelines/lineage)

A minimal manifest for the fixture is shown below. The materialization handoff contributes the producer details separately. Training uses the same `dataset_version` identifier handed to it by the build.

```json
{
  "schema_version": 1,
  "model": "staffing-forecast",
  "version": "v17",
  "pipeline_run": "projects/workforce-demo/locations/us-central1/pipelineJobs/train-042",
  "task": "train",
  "execution": "projects/workforce-demo/locations/us-central1/metadataStores/default/executions/481",
  "training_commit": "90c19d9d6f10b60919d40d73eb4d7a60c9fdcd7f",
  "prediction_cutoff": "2026-09-01T00:00:00Z",
  "dataset_version": "workforce-demo.frozen.shift_inputs_b042@2026-09-02T02:00:00Z",
  "inputs": [{"field": "hours_at_cutoff", "role": "feature", "position": 0}],
  "artifact": {
    "uri": "gs://demo-models/staffing/v17/model.bin",
    "generation": "1788318000000000",
    "sha256": "0402ca8883ee82eb2a43dc830b135d7bab998ee92e23f99548af0c878b92c10f"
  }
}
```

Here `dataset_version` is a catalog identifier pairing a snapshot table with its snapshot time. The prediction cutoff is a separate business fact. An SME can explain whether corrections made after that cutoff should be excluded.

### BigQuery query history

Ingest query jobs from the regional `INFORMATION_SCHEMA.JOBS_BY_PROJECT` view for each approved **execution** project and location. Include the Dataform repository's execution project even when the tables live elsewhere. Run each metadata query in the view's region. Request `bigquery.jobs.listAll` for the observed project and `bigquery.jobs.create` where the connector runs its own query. BigQuery exposes up to 180 days of job history. Start with a bounded backfill such as seven days, configurable within the source's available window. [BigQuery job schema, permissions and retention](https://docs.cloud.google.com/bigquery/docs/information-schema-jobs) · [Dataform job correlation](https://docs.cloud.google.com/dataform/docs/monitor-runs)

- **Incremental sync.** Query all pages for a bounded UTC `creation_time` interval, initially with `job_type = 'QUERY'`. Upsert by the composite job key, re-read a configurable overlap, and refresh unfinished jobs until terminal. Serialize checkpoint updates per scope. Commit each batch's observations together and advance the completed window only after all pages succeed. Exclude the collector's own labeled jobs from usage. Configure Postgres retention for ad hoc history separately from the minimal job evidence pinned to model traces.
- **Facts and gaps.** Use reported table references for observed usage. Record a durable write only for a successful statement with evidence of its actual destination. Ordinary SELECT results can have temporary destinations, cache hits can lack referenced tables, and failed jobs can have incomplete metadata. Preserve their state and mark missing coverage. For scripts, retain the parent and child IDs. Attach the Dataform action to Google's returned job ID and follow children for statement-level evidence. Aggregate bytes and usage at one level to avoid double counting.
- **What scientists can retrieve.** `odcl history table workforce-demo.features.shift_inputs --since 7d` returns matching jobs, time, operation, known Dataform invocation and coverage. Add `--patterns` to group reusable query examples by frequency and last execution. Preserve BigQuery's `normalized_literals` hash when available. A scientist can cite a job and its SQL pattern in an EDA finding. The model trace drills into its exact producing job through the same read. Only the training manifest establishes which fields entered the model.
- **SQL text.** Offer per-scope capture modes. The recommended team setting stores parameterized SQL templates: parse GoogleSQL, remove comments, replace literals and parameter values, preserve allowed table/field names and joins, and retain the template once. A metadata-only mode reads job fields and references without requesting SQL. An explicitly configured full-text mode stores original SQL in a restricted Postgres relation with its own access and retention rules. Keep raw SQL, user emails and full error messages out of general search and application logs. Opaque scripts, unknown identifiers and failed redaction fall back to metadata-only for that job. Permission-filter templates and jobs before grouping or exposing examples.

## Build order

| Change | Work in this repository | Acceptance check |
| --- | --- | --- |
| **1. Specify one trace and port the store** | Add sanitized Google-shaped fixtures for the example and an expected `explain` result. Introduce ordered SQL migrations and psycopg in [the package configuration](../pyproject.toml). Replace the SQLite connection, FTS5 and per-entry batch commits in [the store](../src/odcl/store.py). Use one Postgres DSN in development, CI and deployment. Run a Postgres service in [CI](../.github/workflows/ci.yml) and isolate tests with schemas. | On an empty database, migrations apply once. Existing dbt re-import, search, revision, CLI and MCP behavior passes against Postgres. Existing SQLite files can be imported once in read-only mode, including old revision bodies, pinned links and heads. Verify counts and a historical `uses` result before cutover. |
| **2a. Import BigQuery schemas and job history** | Add `bigquery.py` beside [the dbt importer](../src/odcl/dbt.py). Sync selected schemas and one regional window of jobs. Implement the Postgres checkpoint, table-history read and SQL template mode. Enable restricted original-text reads only after access controls are in place. | Import a cross-project read, a script with a child write, a cache hit and a failed query. Repeat the window and interrupt pagination. Every job appears once after retry and usage totals agree with the fixture. Queries differing only by employee-literal values resolve to one parameterized example. The literals are absent from its API response and logs. |
| **2b. Join one Dataform build to its BigQuery job** | Add `dataform.py` and extract the dbt importer's reusable SQLGlot expression tracing. Add one Postgres operation to persist a verified build handoff, its pinned output fields and their input evidence atomically. Start with explicit table/invocation IDs before adding discovery. | The raw and derived columns resolve once across importers. The fixture yields the expected raw field, SQLX file, expression, commit, successful job from query history and snapshot. Importing job history before or after the invocation gives the same trace. A skipped action yields no successful materialization. Unresolved SQL yields a partial trace. |
| **3. Import one Vertex run and explain one model** | Add `vertex.py` for named run/task reads and a small training-manifest writer. In one transaction, validate the producing task, immutable artifact, input version and selected fields, then write the build. Implement `explain_model(project, version)` on the existing Python interface and expose `odcl --json explain model staffing-forecast --version v17` in [the CLI](../src/odcl/cli.py). | Run the first live trace on one approved pipeline. Match every hop to Google metadata and the trainer's manifest. Train v18 after the SQL changes and verify v17 still resolves to its original evidence. An identical replay is a no-op. A different digest for v17 fails. A missing dependency names the missing key and can be retried without partial writes. |
| **4. Add cards, findings and shared access** | Render a dataset card from grain, population, availability, source schema, query usage and known issues. Render a model-version card from build inputs, evaluation evidence, intended use and limitations. Store SME findings as revisioned Markdown with source, reviewer, scope and links. Let a build pin a specific finding revision. Add a thin HTTP API and [MCP tools](../src/odcl/mcp.py) over the same Python reads and writes. | A second scientist can start at a model card, find the SME explanation and see which model versions used it before it was corrected. CLI, API and MCP return the same trace and scoped query history for the same caller. A later import refreshes technical fields and preserves authored text. |

### First pull request

Start with the golden fixture and Postgres port in change 1. Add the test asserting the eventual trace alongside the implementation that first satisfies it in change 3. Preserve the public operations exercised by [the catalog tests](../tests/test_catalog.py) and [the MCP test](../tests/test_mcp.py). Use a migration transaction and a schema version table. The runtime connects exclusively to Postgres.

Use **Open Data Context Layer** for the project name, **`odcl`** for the CLI and **`open-data-context-layer`** for the proposed distribution. Introduce `ODCL_DATABASE_URL`. Treat moving the Git checkout, renaming the hosted repository and publishing the package as separate release actions.

### Interfaces after the first trace

| Python operation | CLI | HTTP API | MCP tool |
| --- | --- | --- | --- |
| `explain_model(name, version)` | `odcl explain model NAME --version VERSION` | `GET /v1/models/{name}/versions/{version}/trace` | `explain_model` |
| `table_history(key, since, patterns)` | `odcl history table KEY --since 7d --patterns` | `GET /v1/query-history?table=…&since=…&patterns=true` | `get_query_history` |
| `finding_impact(key, revision)` | `odcl impact finding KEY --revision REV` | `GET /v1/findings/{key}/revisions/{rev}/impact` | `finding_impact` |

Build the Python operation and CLI first. Return a common result with pinned IDs, source evidence and a list of missing links. Add pagination for job lists. Delegate HTTP and MCP reads to those operations. Authenticate training publishers separately for manifest writes.

### Operating the pilot

- **Retries and concurrency.** Use native IDs and database uniqueness constraints for every upsert. Lock an entry before moving its head. An older observation may fill historical evidence but must not roll back a newer head or a terminal run state. Accept replays from previously saved manifests; reject conflicting immutable payloads with an actionable error.
- **Ownership and access.** BigQuery owns physical schema, Dataform owns the compiled definition, and scientists own card text and findings. Apply updates to those fields independently. Run the first trace with team-approved metadata and scoped Google read identities. Before shared access, authenticate users and training publishers, enforce the same permissions on documents, search and traversed links, and derive the actor from the authenticated identity.
- **Exit condition.** Given only the model name and version, another scientist can reproduce the example trace and distinguish recorded execution evidence, parsed lineage and unknowns. Run the test after a second build, after replaying imports out of order and after changing a finding. A single Postgres backup and restore must recover the catalog's answer.
