"""The `fathom` command. Every subcommand calls one method of `Fathom` and prints what it answers."""
from __future__ import annotations

import argparse
import json
import sys

from .store import Fathom, FathomError


def flag(entry) -> str:
    return "  (changed since)" if entry.get("changed_since") else ""


def lines(command: str, result) -> list[str]:
    """A person's view of a result. `--json` prints the result itself."""
    if command == "find":
        return [f"{r['key']}" + (f"  [{r['layer']}]" if r.get("layer") else "") + (f"  used by {r['used_by']}" if r.get("used_by") else "")
                + (f"\n    {r['description']}" if r["description"] else "") for r in result] or ["Nothing found."]
    if command == "used-by":
        return [f"{result['key']} is used by {len(result['projects'])} project(s)"] + [
            f"  {p['project']}  {p['how']}" + (f" through {p['through']}" if p["how"] != "direct" else "")
            + (f"  for: {p['purpose']}" if p["purpose"] else "") + flag(p) for p in result["projects"]]
    if command == "uses":
        out = [f"{result['key']} uses {len(result['uses'])} table(s) or column(s)"] + [
            f"  {u['key']}" + (f"  for: {u['purpose']}" if u.get("purpose") else "") + flag(u) for u in result["uses"]]
        if "built_from" in result:
            out += ["built from:"] + [f"  {n['key']}" for n in result["built_from"]]
        return out
    if command == "lineage":
        out = [result["key"]]
        for direction in ("upstream", "downstream"):
            if direction in result:
                out += [direction + ":"] + ([f"  {'  ' * (n['depth'] - 1)}{n['key']}" + flag(n) for n in result[direction]] or ["  nothing"])
        return out
    if command == "import":
        return [f"{result['added']} added, {result['changed']} changed, {result['unchanged']} unchanged"]
    if command in ("add", "use"):
        return [f"{result['key']}  {result['change']}  ({result['revision']})"]
    return [json.dumps(result, indent=2)]


def run(fathom: Fathom, args):
    if args.command == "import":
        from .dbt import dbt_entries
        return fathom.apply(dbt_entries(args.dbt, args.select))
    if args.command == "add":
        fields = {k: v for k, v in vars(args).items() if k in ("path", "table", "name", "description", "system", "layer", "owner", "type", "dtype",
                                                                 "definition", "repository", "status", "derived_from", "tags") and v not in (None, [])}
        return fathom.put(args.kind, fields)
    if args.command == "find":
        return fathom.find(" ".join(args.words), args.kind, args.layer, args.limit)
    if args.command == "show":
        return fathom.get(args.key)
    if args.command == "lineage":
        return fathom.lineage(args.key, args.direction)
    if args.command == "use":
        return fathom.use(args.project, args.keys, args.purpose)
    if args.command == "used-by":
        return fathom.used_by(args.key)
    if args.command == "uses":
        return fathom.uses(args.project, args.upstream)
    return fathom.overview()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fathom", description="What data exists, how it connects, and which projects use it.")
    p.add_argument("--db", help="the Fathom database (default: $FATHOM_DB, else ~/.fathom/fathom.db)")
    p.add_argument("--as", dest="actor", help="who is registering (default: $FATHOM_USER, else your login name)")
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("overview", help="how much is registered, and what changed after a project used it")

    s = sub.add_parser("import", help="read tables, columns and lineage from a dbt project")
    s.add_argument("--dbt", required=True, metavar="PATH", help="a dbt target folder or its manifest.json")
    s.add_argument("--select", nargs="+", metavar="MODEL", help="only these models and what they are built from")

    s = sub.add_parser("add", help="register a table, a column or a project by hand")
    kinds = s.add_subparsers(dest="kind", required=True)
    t = kinds.add_parser("table")
    t.add_argument("path", help="database.schema.name")
    for name in ("system", "layer", "type"):
        t.add_argument("--" + name)
    c = kinds.add_parser("column")
    c.add_argument("table")
    c.add_argument("name")
    c.add_argument("--dtype")
    c.add_argument("--definition", help="how it is computed")
    j = kinds.add_parser("project")
    j.add_argument("name")
    j.add_argument("--repository")
    j.add_argument("--status")
    for k in (t, c, j):
        k.add_argument("--description", required=k is j)
        k.add_argument("--owner")
        k.add_argument("--tags", nargs="+", default=[])
    for k in (t, c):
        k.add_argument("--derived-from", nargs="+", default=[], metavar="KEY", help="what it is built from")

    s = sub.add_parser("find", help="search tables, columns and projects")
    s.add_argument("words", nargs="*")
    s.add_argument("--kind", choices=["table", "column", "project"])
    s.add_argument("--layer")
    s.add_argument("--limit", type=int, default=50)

    s = sub.add_parser("show", help="one entry in full")
    s.add_argument("key")

    s = sub.add_parser("lineage", help="what a table or column is built from, and what is built from it")
    s.add_argument("key")
    s.add_argument("--direction", choices=["up", "down", "both"], default="both")

    s = sub.add_parser("use", help="record that a project used tables or columns")
    s.add_argument("project")
    s.add_argument("keys", nargs="+", metavar="KEY")
    s.add_argument("--for", dest="purpose", help="what the project used them for")

    s = sub.add_parser("used-by", help="which projects depend on a table or column")
    s.add_argument("key")

    s = sub.add_parser("uses", help="what a project used")
    s.add_argument("project")
    s.add_argument("--upstream", action="store_true", help="also everything that data is built from")

    s = sub.add_parser("mcp", help="serve Fathom to an assistant over the Model Context Protocol")
    s.add_argument("--allow-writes", action="store_true", help="also offer the tools that register projects and record use")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        with Fathom(args.db, args.actor) as fathom:
            if args.command == "mcp":
                from .mcp import main as serve
                return serve(fathom, args.allow_writes)
            result = run(fathom, args)
    except FathomError as e:
        print(f"fathom: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2) if args.json else "\n".join(lines(args.command, result)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
