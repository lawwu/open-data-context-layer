# Commands

Every `odcl` command, and how each one reaches the catalog.

## Commands

| Command | Answers |
|---|---|
| `odcl find WORDS [--kind] [--layer] [--retired]` | What data is there for this? |
| `odcl show KEY` | Everything about one entry, with its history |
| `odcl lineage KEY` | What is it built from, and what is built from it? |
| `odcl diff KEY [--since REV\|TIME] [--until REV\|TIME]` | What changed, and does it break what uses it? |
| `odcl import --quality FILE.jsonl` or `--dbt-tests TARGET`; `odcl quality KEY [--at TIME]` | Was it passing its tests, now or then? |
| `odcl term add`, `odcl domain add`, `odcl means`, `odcl in-domain`, `odcl glossary` | What does this column mean in the business's words, and whose is it? |
| `odcl popular [--since 30d]` | Which tables are actually queried? |
| `odcl impact [--project P]` | What is each project exposed to: data that changed, is gone, or is failing a test? |
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
| `odcl tables`, `odcl projects`, `odcl models`, `odcl runs`, `odcl graph KEY` | The lists and the lineage graph the web page shows |
| `odcl migrate` | Create or update the catalog's tables |
| `odcl import --openlineage FILE` | Read OpenLineage events: runs and the lineage they establish |
| `odcl import --docs PATH... [--repo-url URL] [--commit SHA]` | Read Markdown pages that describe catalog entries |
| `odcl docs KEY` | Which documents describe this entry? |
| `odcl serve [--accept-lineage] [--require-sign-in]` | Serve the catalog over HTTP, with a web page to browse it |
| `odcl mcp [--allow-writes] [--server URL]` | Serve all of this to an assistant |
| `odcl users list\|add\|set\|token\|tokens\|revoke\|whoami` | Who may read and write, and their API tokens |

Add `--json` before the command for the full result. A bare path such as `churn.bronze.accounts` works wherever a key does, when only one entry has it.

The database is one PostgreSQL database: `--database-url`, else `$ODCL_DATABASE_URL`. Run `odcl migrate` once on a new database and again after upgrading. With `$ODCL_TOKEN` set to an API token, the command signs in as that token's user, records changes under their name and checks their role (see [Users and sign-in](serving.md#users-and-sign-in)). Without one it is a direct connection, trusted because it holds the database's credentials, and changes are recorded under `--as`, else `$ODCL_USER`, else your login name.

With `--server URL` (or `$ODCL_SERVER`) the command works through an `odcl serve` instead, so a person needs no database credentials: reads and writes go over its HTTP API, signed in with `$ODCL_TOKEN`, recorded under that user and checked against their role by the server. What is worked out on your machine is worked out before it is sent, such as `odcl created-by` reading a local file's repository and commit from git. Commands that read from where the data is (`import`, `sync`, `handoff`, `record`, `discover`, `propose`, `add table`, `add column`, `privacy apply`) need a database connection and say so, as do options that write a local file, such as `privacy propose --write`.
