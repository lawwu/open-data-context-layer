"""SQL definitions from anywhere: one JSON object per line, each a table and the SELECT that builds it.

A registry, a hand-kept list of views, or a tool odcl has no importer for can write this file, and odcl reads it
the way it reads a Dataform compilation result: each column is traced through the SELECT to the columns it is
computed from, `SELECT *` expands to what the catalog knows of the tables read, and a column traced only part of
the way is tagged `lineage:partial`.

    {"target": "proj.ds.orders_summary", "select": "SELECT region, SUM(amount) AS total FROM proj.ds.orders GROUP BY region",
     "dependencies": ["proj.ds.orders"], "description": "Totals by region", "type": "view", "file": "views/summary.sql",
     "columns": {"total": "Order amount, summed"}}
    {"target": "proj.ds.orders", "declaration": true, "description": "Orders, as the billing system loads them"}

`target` is required, and so is `select` unless the line is a `declaration` (a source table, with no SQL). Tables
are named as the catalog keys them, database.schema.name (or schema.name), and `dependencies` adds tables the
SELECT does not name itself. Entries are merged with what other importers said, under the name `sql_definitions`
in `source`, and the whole file is applied together.
"""
from __future__ import annotations

import json
from pathlib import Path

from .store import Catalog, CatalogError, require

IMPORTER = "sql_definitions"
FIELDS = {"target", "select", "dependencies", "description", "columns", "type", "file", "declaration"}


def _target(name, where: str) -> dict:
    require(isinstance(name, str) and 2 <= len(name.strip().split(".")) and all(name.strip().split(".")), "sql_definitions",
            f"{where}: name a table as database.schema.name or schema.name")
    *database, schema, table = name.strip().split(".")
    return {"database": ".".join(database), "schema": schema, "name": table}


def action(line: dict, where: str) -> dict:
    """One definition in the shape of a Dataform compilation result action, which `dataform.Definitions` reads."""
    require(isinstance(line, dict), "sql_definitions", f"{where}: each line is a JSON object")
    unknown = set(line) - FIELDS
    require(not unknown, "sql_definitions", f"{where}: unknown field(s) {', '.join(sorted(unknown))}; use {', '.join(sorted(FIELDS))}")
    target = _target(line.get("target"), where)
    description = line.get("description")
    require(description is None or isinstance(description, str), "sql_definitions", f"{where}: description is text")
    if line.get("declaration"):
        require(not line.get("select"), "sql_definitions", f"{where}: a declaration has no select")
        return {"target": target, "filePath": line.get("file"), "declaration": {"relationDescriptor": {"description": description}}}
    require(isinstance(line.get("select"), str) and line["select"].strip(), "sql_definitions", f"{where}: select is the SQL that builds the table")
    dependencies = line.get("dependencies") or []
    require(isinstance(dependencies, list), "sql_definitions", f"{where}: dependencies is a list of tables")
    columns = line.get("columns") or {}
    if isinstance(columns, list):  # [{"name": ..., "description": ...}] is accepted as well as {name: description}
        require(all(isinstance(c, dict) and c.get("name") for c in columns), "sql_definitions", f"{where}: each column has a name")
        columns = {c["name"]: c.get("description") for c in columns}
    require(isinstance(columns, dict), "sql_definitions", f"{where}: columns maps each column's name to its description")
    return {"target": target, "filePath": line.get("file"), "relation": {
        "selectQuery": line["select"], "dependencyTargets": [_target(d, where) for d in dependencies], "relationType": line.get("type") or "table",
        "relationDescriptor": {"description": description, "columns": [{"path": str(name).split("."), "description": text}
                                                                       for name, text in columns.items()]}}}


def read(path: str) -> list[dict]:
    """Every definition in a JSON Lines file, as actions. Blank lines are skipped."""
    file = Path(path).expanduser()
    require(file.is_file(), "sql_definitions", f"{path} is not a file")
    out = []
    for n, text in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
        if not text.strip():
            continue
        try:
            line = json.loads(text)
        except json.JSONDecodeError as e:
            raise CatalogError("sql_definitions", f"{file.name} line {n} is not JSON: {e.msg}") from None
        out.append(action(line, f"{file.name} line {n}"))
    require(out, "sql_definitions", f"{path} has no definitions")
    targets = [".".join(p for p in a["target"].values() if p).lower() for a in out]
    twice = sorted({t for t in targets if targets.count(t) > 1})
    require(not twice, "sql_definitions", f"{path} defines {', '.join(twice)} more than once")
    return out


def import_definitions(catalog: Catalog, path: str, *, repository: str | None = None, system: str = "bigquery", dialect: str = "bigquery") -> dict:
    """Register every table a definitions file names, and each column with what it is computed from. All, or none."""
    from .dataform import definition_entries
    actions = read(path)
    found = definition_entries(catalog, repository or Path(path).name, actions, importer=IMPORTER, system=system, dialect=dialect)
    counts = catalog.apply(found.entries, merge=True)
    traced = [status for columns in found.traced.values() for status in columns.values()]
    return {**counts, "definitions": len(actions), "columns_traced": traced.count("resolved"), "columns_partial": traced.count("partial")}
