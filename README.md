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

## Install

odcl is not on PyPI. Install a release from GitHub (`@latest`, or a specific `@vX.Y.Z`), or run the Docker image:

```sh
uv tool install "open-data-context-layer[mcp] @ git+https://github.com/lawwu/open-data-context-layer@latest"
```

```sh
docker run -d -p 127.0.0.1:8080:8080 \
  -e ODCL_DATABASE_URL=postgresql://odcl:secret@db.example.com/odcl \
  ghcr.io/lawwu/open-data-context-layer:0.3
```

The catalog is one PostgreSQL database, named by `--database-url` or `$ODCL_DATABASE_URL`. Run `odcl migrate` on a new database and after upgrading (the image does it on start). [`CHANGELOG.md`](CHANGELOG.md) says what each release changed.

## Documentation

The same pages are on the [docs site](https://lawwu.github.io/open-data-context-layer/).

| Page | What it covers |
|---|---|
| [Commands](docs/commands.md) | Every `odcl` command, `--json`, and working through a server with `--server` |
| [Importing](docs/importing.md) | dbt, BigQuery, PostgreSQL, Dataform and plain SQL; what an import reports; retiring what is gone; foreign keys and copies |
| [Describing data](docs/describing-data.md) | Glossary terms and domains, Markdown documents, privacy fields |
| [Changes, quality and alerts](docs/changes-quality-alerts.md) | What changed and whether it breaks its users, test results, alert subscriptions |
| [Models and runs](docs/models-and-runs.md) | Dataset versions, model versions from Vertex AI Pipelines, OpenLineage |
| [Serving, the web page and sign-in](docs/serving.md) | `odcl serve`, the JSON API, the lineage explorer, Docker, users, API tokens, single sign-on |
| [For agents and from Python](docs/agents-and-python.md) | `odcl mcp` and the `Catalog` class |
| [Working with several people](docs/working-with-several-people.md) | Roles, and who should import and who should edit |
| [Developing and releases](docs/developing.md) | Tests, how a release is cut, what is not built yet |
| [Implementation plan](docs/implementation-plan.md) | Where it is going |
