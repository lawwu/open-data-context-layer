"""Column lineage from compiled SQL, shared by every importer that has a model's or an action's query.

sqlglot reads the SQL, so nothing here parses SQL by hand. A column traced to every input it reads is
`resolved`. One traced only part of the way, or not at all, says so: the catalog never presents a guess as lineage.
"""
from __future__ import annotations

import logging

MAX_SQL = 100_000


def relation(table) -> str:
    """project.dataset.table for a sqlglot table node, lower-cased."""
    return ".".join(p for p in (table.catalog, table.db, table.name) if p).lower()


def selected(sql: str, dialect, schema: dict | None = None) -> list[str]:
    """The names a query's outermost SELECT gives its columns, lower-cased. With the input tables' columns in
    `schema`, a `*` is expanded; without them it names nothing. Empty when the SQL cannot be read."""
    tree = parse(sql, dialect)
    if tree is None:
        return []
    if schema and "*" in tree.named_selects:
        from sqlglot.optimizer.qualify import qualify
        try:
            tree = qualify(tree, schema=schema, dialect=dialect, validate_qualify_columns=False, quote_identifiers=False)
        except Exception:  # noqa: BLE001 - a star that cannot be expanded names nothing
            pass
    return [name.lower() for name in tree.named_selects if name != "*"]


def parse(sql: str, dialect):
    """The query sqlglot reads from `sql`, or None when it is not one query sqlglot can read."""
    import sqlglot
    from sqlglot import exp
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:  # noqa: BLE001 - unreadable SQL is reported by the caller, not raised
        return None
    return tree if isinstance(tree, exp.Query) else None


def tables(sql: str, dialect) -> list[str]:
    """Every table a query reads, as project.dataset.table, lower-cased. Names of its own CTEs are left out."""
    from sqlglot import exp
    tree = parse(sql, dialect)
    if tree is None:
        return []
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    return sorted({relation(t) for t in tree.find_all(exp.Table) if t.name and relation(t) not in ctes})


def as_written(column: str, sql: str, dialect) -> str | None:
    """The expression the outermost SELECT gives `column`, as the query writes it (without qualifying names)."""
    from sqlglot import exp
    tree = parse(sql, dialect)
    for item in tree.selects if tree is not None else []:
        if item.alias_or_name.lower() == column.lower() and not isinstance(item, exp.Star):
            return (item.this if isinstance(item, exp.Alias) else item).sql(dialect=dialect)
    return None


def column_parents(column: str, sql: str, dialect, schema: dict) -> tuple[str | None, list[tuple[str, str]], bool]:
    """(the column's expression, [(upstream relation, column)], whether every input was traced) from compiled SQL.

    A row count has no input column. It is traced to the filter and grouping columns of the table it counts, and
    reported as not fully traced so the entry says so."""
    from sqlglot import exp
    from sqlglot.lineage import lineage
    node = lineage(column, sql, schema=schema or None, dialect=dialect)
    nodes = list(node.walk())
    leaves = {(relation(n.source), n.name.split(".")[-1]) for n in nodes if isinstance(n.source, exp.Table)}
    expression = as_written(column, sql, dialect)
    if expression is None:  # a column reached through `*` has no select item of its own
        expression = node.expression.this.sql(dialect=dialect) if isinstance(node.expression, exp.Alias) else node.expression.sql(dialect=dialect)
    if leaves:
        return expression, sorted(leaves), True
    for inner in reversed(nodes):  # nothing traced: the deepest select that reads exactly one table
        source = inner.source
        tables = list(source.find_all(exp.Table)) if isinstance(source, exp.Select) else []
        if len(tables) == 1 and not tables[0].find_ancestor(exp.Subquery):
            used = [c for clause in ("where", "group") if source.args.get(clause) for c in source.args[clause].find_all(exp.Column)]
            return expression, sorted({(relation(tables[0]), c.name) for c in used}), False
    return expression, [], False


def template(sql: str | None, dialect: str | None = "bigquery") -> str | None:
    """A query with its comments removed and every literal and parameter replaced by `?`, or None when that
    cannot be done for certain. Table and field names stay, so the shape still says what it reads."""
    if not sql or len(sql) > MAX_SQL:
        return None
    import sqlglot
    from sqlglot import exp
    quiet = logging.getLogger("sqlglot")
    was, quiet.disabled = quiet.disabled, True  # sqlglot quotes the SQL it cannot read in its warnings
    try:
        statements = sqlglot.parse(sql, dialect=dialect)
        if len(statements) != 1 or statements[0] is None or isinstance(statements[0], exp.Command):
            return None  # a script, or syntax sqlglot keeps as opaque text
        tree = statements[0]
        for node in list(tree.find_all(exp.In)):
            if node.expressions and all(isinstance(e, (exp.Literal, exp.Neg)) for e in node.expressions):
                node.set("expressions", [exp.Placeholder()])  # a longer list of values is the same question
        for node in list(tree.find_all(exp.Literal, exp.RawString, exp.ByteString, exp.HexString, exp.BitString, exp.National, exp.Parameter)):
            node.replace(exp.Placeholder())
        out = tree.sql(dialect=dialect, comments=False)
    except Exception:  # noqa: BLE001 - SQL that cannot be read is kept as metadata only
        return None
    finally:
        quiet.disabled = was
    return None if "'" in out or '"' in out or len(out) > MAX_SQL else out  # a quote left behind means a literal may be too


# ---- job history ---------------------------------------------------------------------------------------------
