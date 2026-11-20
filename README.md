# Fathom

A data context layer: what data exists, how it connects, and which projects use it. It is for people and for AI agents, so both can find the right data and see what a change would affect.

Fathom holds descriptions. It does not store or serve the data itself.

## What it keeps

| Entry | What it is | Key |
|---|---|---|
| table | A table, view or file in some system | `table:<database>.<schema>.<name>` |
| column | One column of a registered table | `column:<table path>.<name>` |
| project | Anything that uses data: an analysis, a model | `project:<name>` |

Data is registered once and shared. It does not belong to a project. Two links connect entries:

- **derived_from**: what a table or column is built from (lineage).
- **uses**: the tables and columns a project used, with what for.

A `uses` link remembers the revision of the data the project saw. If the data is redefined later, Fathom says so until the project records use again.

Every change is a new revision with who made it and when.

## Try it

```sh
uv sync --all-extras
uv run fathom import --dbt tests/fixtures/dbt_medallion/target
uv run fathom add project churn-model --description "Predict which accounts cancel within 90 days"
uv run fathom use churn-model churn.main_gold.account_features.plan_tier --for "training feature"
uv run fathom find plan
uv run fathom used-by churn.bronze.accounts.plan
uv run fathom uses churn-model --upstream
uv run fathom lineage churn.main_gold.account_features.tenure_months
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
| `fathom find WORDS [--kind] [--layer]` | What data is there for this? |
| `fathom show KEY` | Everything about one entry, with its history |
| `fathom lineage KEY` | What is it built from, and what is built from it? |
| `fathom used-by KEY` | Which projects depend on it? |
| `fathom uses PROJECT [--upstream]` | What did this project use? |
| `fathom use PROJECT KEY... [--for TEXT]` | Record that a project used data |
| `fathom add table\|column\|project ...` | Register by hand |
| `fathom import --dbt PATH [--select MODEL...]` | Read a dbt project |
| `fathom overview` | Counts, and data that changed after a project used it |
| `fathom mcp [--allow-writes]` | Serve all of this to an assistant |

Add `--json` before the command for the full result. A bare path such as `churn.bronze.accounts` works wherever a key does, when only one entry has it.

The database is one SQLite file: `--db`, else `$FATHOM_DB`, else `~/.fathom/fathom.db`. Changes are recorded under `--as`, else `$FATHOM_USER`, else your login name. There is no sign-in.

## Importing from dbt

`fathom import --dbt target/` reads `manifest.json` (run `dbt compile` or `dbt run` first), and `catalog.json` for column types when `dbt docs generate` has written it.

- Every source and model becomes a table, with its layer (from `meta.layer`, a bronze/silver/gold tag, or the schema name), owner (`meta.owner`) and description.
- Table lineage comes from dbt's dependency graph. Column lineage is read from each model's compiled SQL with [sqlglot](https://github.com/tobymao/sqlglot).
- Each entry records the file that defines it and the commit that last changed that file, with a link when the repository has a web address.
- Importing again changes nothing unless something changed. Redefining a table does not create new revisions of everything built from it; those entries report that their input changed.
- A column that sqlglot cannot trace fully (a row count, for example) is tagged `lineage:partial`.

## For agents

```sh
uv run fathom mcp
```

serves `find_data`, `get_entry`, `trace_lineage`, `used_by` and `project_uses` over the Model Context Protocol (the official [MCP SDK](https://github.com/modelcontextprotocol/python-sdk)). With `--allow-writes` it also offers `register_project` and `record_use`.

## From Python

```python
from fathom_data import Fathom

with Fathom() as fathom:
    fathom.find("plan", layer="gold")
    fathom.used_by("churn.bronze.accounts.plan")
```

`Fathom` is the whole interface: `put`, `apply`, `use`, `find`, `get`, `lineage`, `used_by`, `uses`, `overview`.

## Not built yet

- The machine-learning shape on top: training and serving features, labels, training datasets and model cards.
- Importers for Dataform, Postgres, BigQuery and Denodo.
- A web page.
- A shared server. Today a team shares Fathom by sharing the database file.

The Python package is `fathom-data` (the name `fathom` on PyPI belongs to another project); the command and the repository are `fathom`.
