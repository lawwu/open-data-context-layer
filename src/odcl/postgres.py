"""PostgreSQL into the catalog: tables, views and their columns, view lineage, and foreign keys.

Everything comes from the system catalog of the database named by a connection URL, with plain reads: no table's
rows are read. Keys are database.schema.table, lower-cased. The URL, and the password in it, are never kept.

- **Tables** (ordinary, partitioned, foreign) and **views** (and materialized views) become table entries, with
  their comments as descriptions; their columns, with types, nullability and comments, become column entries.
- **View lineage.** Each view's SQL is read with `pg_get_viewdef` under an empty search_path, so PostgreSQL names
  every table it reads with its schema. The database is added, and each column is traced through the SQL the way a
  Dataform definition is (`dataform.Definitions`, in PostgreSQL's dialect), tagged `lineage:partial` when only
  partly traced.
- **Foreign keys** become `references` links, column by column. A key to a schema not read is left out and named.

Entries merge with what other importers said, under `postgres` in `source`, and are applied together.
"""
from __future__ import annotations

from .lineage import parse
from .store import COLUMN, PATH, Catalog, CatalogError, connect, reference_entries, require

KINDS = {"r": "table", "p": "table", "f": "foreign table", "v": "view", "m": "materialized view"}
SKIP = ("pg_catalog", "information_schema", "pg_toast")


def read(dsn: str, schemas: list[str] | None = None) -> dict:
    """What the system catalog says about the relations of these schemas (default: every schema but the system's)."""
    require(dsn, "postgres", "Name the PostgreSQL database to read: --postgres postgresql://user@host/dbname")
    try:
        db = connect(dsn, autocommit=True)
    except CatalogError as e:
        raise CatalogError("postgres", str(e)) from None
    with db:
        db.execute("SET search_path = ''")  # so pg_get_viewdef names every relation with its schema
        database = db.execute("SELECT current_database() AS name").fetchone()["name"]
        where = "n.nspname = ANY(%(schemas)s)" if schemas else "n.nspname <> ALL(%(skip)s) AND n.nspname NOT LIKE 'pg_temp%%' AND n.nspname NOT LIKE 'pg_toast%%'"
        params = {"schemas": schemas, "skip": list(SKIP)}
        relations = db.execute(f"""
            SELECT c.oid, n.nspname AS schema, c.relname AS name, c.relkind AS kind, obj_description(c.oid, 'pg_class') AS comment,
                   CASE WHEN c.relkind IN ('v', 'm') THEN pg_get_viewdef(c.oid, true) END AS sql
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'f', 'v', 'm') AND NOT c.relispartition AND {where}
            ORDER BY n.nspname, c.relname""", params).fetchall()
        columns = db.execute(f"""
            SELECT a.attrelid AS oid, a.attname AS name, a.attnum AS position, format_type(a.atttypid, a.atttypmod) AS dtype,
                   a.attnotnull AS not_null, col_description(a.attrelid, a.attnum) AS comment
            FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'p', 'f', 'v', 'm') AND NOT c.relispartition AND {where}
            ORDER BY a.attrelid, a.attnum""", params).fetchall()
        keys = db.execute(f"""
            SELECT k.conname AS name, n.nspname AS schema, c.relname AS name_from, fn.nspname AS to_schema, f.relname AS name_to,
                   ARRAY(SELECT a.attname FROM unnest(k.conkey) WITH ORDINALITY u(n, i)
                         JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = u.n ORDER BY u.i) AS from_columns,
                   ARRAY(SELECT a.attname FROM unnest(k.confkey) WITH ORDINALITY u(n, i)
                         JOIN pg_attribute a ON a.attrelid = k.confrelid AND a.attnum = u.n ORDER BY u.i) AS to_columns
            FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_class f ON f.oid = k.confrelid JOIN pg_namespace fn ON fn.oid = f.relnamespace
            WHERE k.contype = 'f' AND k.conparentid = 0 AND NOT c.relispartition AND {where} ORDER BY n.nspname, c.relname, k.conname""", params).fetchall()
    return {"database": database, "relations": relations, "columns": columns, "foreign_keys": keys}


def _qualified(sql: str, database: str) -> str | None:
    """A view's SQL with every schema-qualified table also named by its database, as the catalog keys tables."""
    from sqlglot import exp
    tree = parse(sql, "postgres", as_keyed=False)
    if tree is None:
        return None
    for table in tree.find_all(exp.Table):
        if table.args.get("db") and not table.args.get("catalog"):
            table.set("catalog", exp.to_identifier(database))
    return tree.sql(dialect="postgres")


def entries(catalog: Catalog, found: dict, database: str | None = None) -> tuple[list[tuple[str, dict]], list[str], list[str], int]:
    """(kind, payload) entries for what `read` found: relations, columns, view lineage and foreign keys; what was
    skipped, with why; what could not be linked; and how many views were traced.

    Every relation is registered first, then the columns of tables, then each view, after the views it reads, with
    each of its columns in one payload that says both what the catalog read about it and how it is computed."""
    from .dataform import Definitions
    from .lineage import tables as tables_read
    database = (database or found["database"]).lower()
    path = lambda schema, name: f"{database}.{schema}.{name}".lower()
    by_oid = {r["oid"]: r for r in found["relations"]}
    relations, skipped = {}, []
    for r in found["relations"]:
        if path(r["schema"], r["name"]) in relations:  # "Orders" and orders: one key, so the second is left out
            other = relations[path(r["schema"], r["name"])]
            skipped.append(f"{r['schema']}.{r['name']}: the same name as {other['schema']}.{other['name']} once lower-cased")
        elif PATH.match(path(r["schema"], r["name"])):
            relations[path(r["schema"], r["name"])] = r
        else:
            skipped.append(f"{r['schema']}.{r['name']}: not a name the catalog keys")
    tables = {t: ("table", {"path": t, "system": "postgres", "type": KINDS[r["kind"]], "description": (r["comment"] or "")[:4000] or None,
                            "source": {"postgres": {"database": database, "schema": r["schema"], "relation": r["name"], "kind": KINDS[r["kind"]]}}})
              for t, r in relations.items()}
    columns: dict[str, dict[str, dict]] = {t: {} for t in relations}
    for c in found["columns"]:
        r = by_oid.get(c["oid"])
        table = path(r["schema"], r["name"]) if r else None
        if table not in relations or relations[table]["oid"] != c["oid"]:  # a relation skipped above: its columns are too
            continue
        if not COLUMN.match(c["name"].lower()):
            skipped.append(f"{table}.{c['name']}: not a name the catalog keys")
            continue
        if c["name"].lower() in columns[table]:
            skipped.append(f"{table}.{c['name']}: the same name as another column once lower-cased")
            continue
        columns[table][c["name"].lower()] = {"table": f"table:{table}", "name": c["name"].lower(), "dtype": c["dtype"],
                                             "description": (c["comment"] or "")[:4000] or None,
                                             "source": {"postgres": {"position": c["position"], "not_null": c["not_null"]}}}
    sql = {}
    for t, r in relations.items():
        if r["sql"]:
            sql[t] = _qualified(r["sql"], database)
            if sql[t] is None:
                skipped.append(f"{t}: its SQL could not be read, so its lineage is not traced")
    # every relation but a traced view, which is registered once below with what it is built from
    out = [entry for t, entry in tables.items() if not sql.get(t)] + [("column", p) for t, r in relations.items() if not r["sql"] for p in columns[t].values()]

    # views, each after the views it reads, every table it reads already known by its columns
    order, seen = [], set()

    def visit(view: str, path_: tuple = ()) -> None:
        if view in seen or view in path_:
            return
        for read in tables_read(sql[view], "postgres"):
            if sql.get(read):
                visit(read, (*path_, view))
        seen.add(view)
        order.append(view)
    for view in sorted(t for t in sql if sql[t]):
        visit(view)
    definitions = Definitions(catalog, database, importer="postgres", system="postgres", dialect="postgres")
    definitions.tables, definitions.columns = set(relations), {t: set(cs) for t, cs in columns.items()}
    for view in order:
        r = relations[view]
        definitions.relation({"target": {"database": database, "schema": r["schema"].lower(), "name": r["name"].lower()},
                              "relation": {"selectQuery": sql[view], "relationType": KINDS[r["kind"]], "relationDescriptor": {"description": r["comment"]}}})
    traced = set()
    for kind, payload in definitions.entries:
        said = payload["source"]["postgres"] if isinstance(payload.get("source"), dict) and "postgres" in payload["source"] else None
        if kind == "table" and payload["path"] in tables:  # a view: what the catalog read, and the tables it is built from
            physical = tables[payload["path"]][1]
            payload = {**physical, "derived_from": payload.get("derived_from") or [],
                       "source": {"postgres": {**physical["source"]["postgres"], "query_sha256": (said or {}).get("query_sha256")}}}
        elif kind == "column":
            table, name = payload["table"].removeprefix("table:"), payload["name"]
            physical = columns.get(table, {}).get(name)
            if physical and said is not None:  # a view's column: one payload with its type and how it is computed
                traced.add((table, name))
                payload = {**physical, **{k: v for k, v in payload.items() if k not in ("source", "description") and v is not None},
                           "source": {"postgres": physical["source"]["postgres"]}, "description": physical["description"]}
            elif physical:
                continue  # a column of a table read, named by a view: registered above
        out.append((kind, payload))
    out += [("column", p) for t in sql for name, p in columns[t].items() if (t, name) not in traced]  # a view column its SQL did not name
    skipped += [f"{t}: its SQL names no columns sqlglot can read, so its lineage is not traced" for t in definitions.unreadable]

    keys = [(path(k["schema"], k["name_from"]), a.lower(), f"column:{path(k['to_schema'], k['name_to'])}.{b.lower()}")
            for k in found["foreign_keys"] for a, b in zip(k["from_columns"], k["to_columns"], strict=True)]
    registering = {f"column:{t}.{c}" for t, cs in columns.items() for c in cs}
    own = [k for k in keys if f"column:{k[0]}.{k[1]}" in registering]  # a key on a column skipped above is skipped with it
    skipped += [f"foreign key column:{t}.{c} -> {target}: its column was skipped" for t, c, target in keys if f"column:{t}.{c}" not in registering]
    references, unresolved = reference_entries(catalog, own, registering)
    # not in the schemas read: tables a view reads (registered by name only), and columns a foreign key points at (not linked)
    return out + references, skipped, definitions.unresolved + unresolved, len(order)


def import_database(catalog: Catalog, dsn: str, *, schemas: list[str] | None = None, database: str | None = None) -> dict:
    """Register a PostgreSQL database's tables, views, columns, view lineage and foreign keys. All of it, or none."""
    found = read(dsn, schemas)
    made, skipped, unresolved, traced = entries(catalog, found, database)
    name = (database or found["database"]).lower()
    with catalog.importing("postgres", scope={"database": name, "schema": schemas or None}) as imp:
        imp.extend(made)
        imp.unresolved(*unresolved)
        imp.skip(*skipped)
    return {**imp.result, "database": name, "relations": len(found["relations"]), "views_traced": traced, "foreign_keys": len(found["foreign_keys"])}
