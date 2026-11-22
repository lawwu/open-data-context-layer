# For agents and from Python

The catalog as MCP tools and as a Python library.

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

`Catalog` is the whole interface: `put`, `apply`, `use`, `find`, `get`, `lineage`, `used_by`, `uses`, `table_history`, `query_jobs`, `tables`, `projects`, `runs`, `models`, `overview`. `importing(importer, scope=…)` is how an importer writes: it registers what the importer read, withdraws what it said before within that scope and no longer does, and answers the same seven fields for every import (`odcl.imports`). `apply` registers a batch in one transaction: all of it, or none; with `withdraw` it also withdraws what that importer said before within what it read and no longer saw (`Catalog.withdraw`). It takes every entry lock first (512 stripes that keys hash to, in order), so an import of any size fits PostgreSQL's lock table and two imports run one after the other rather than deadlock. With `merge=True`, fields a payload leaves out keep what the entry already says, and the tags a payload sends belong to the one importer named in its `source`, so they replace only that importer's earlier tags. Without `merge` a payload says the whole entry, except what is said beside it by its own commands: copies (`instances`), `created_by`, a column's `references` and the privacy fields are kept when the payload leaves them out, and cleared when it sets them to null. So a dbt import, which does not merge, keeps them. Builds are read and written through `odcl.dataform.import_invocation`, `odcl.builds.record_handoff` and `odcl.builds.explain_dataset`; model versions through `odcl.vertex.import_build` and `Catalog.explain_model`; OpenLineage events through `odcl.openlineage.ingest`. `odcl.server.make_server` returns the HTTP server. `Catalog(token=...)` signs in as an API token's user and checks their role on every write; `odcl.users` manages users and tokens.
