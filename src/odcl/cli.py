"""The `odcl` command: the command-line adapter over `odcl.operations`.

Every operation with a `cli` name is a command here, its params flags and positional arguments as the table spells
them. Three commands are not operations, because they do not run against an open catalog: migrate, serve and mcp.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .operations import GROUPS, OPERATIONS, Operation, operation, prepare
from .store import Catalog, CatalogError, migrate
from .times import moment, span  # noqa: F401 - kept where they were for callers that import them from here


def _add_params(target: argparse.ArgumentParser, op: Operation) -> None:
    groups: dict[str, argparse._MutuallyExclusiveGroup] = {}
    for p in op.params:
        if p.cli is None:
            continue
        options: dict = {"help": p.help or None}
        if p.metavar:
            options["metavar"] = p.metavar
        if p.cli == "positional":
            if p.nargs:
                options["nargs"] = p.nargs
            target.add_argument(p.name, **options)
            continue
        where = target
        if p.group:
            if p.group not in groups:
                groups[p.group] = target.add_mutually_exclusive_group(required=p.group.endswith("!"))
            where = groups[p.group]
        if p.kind == "bool":
            options["action"] = "store_true"
        else:
            if p.kind == "int":
                options["type"] = int
            if p.kind in ("list", "words"):
                options.update({"action": "append"} if p.append else {"nargs": p.nargs or "+"})
            if p.choices:
                options["choices"] = [c for c in p.choices if isinstance(c, str)]
            if p.required and not p.group:
                options["required"] = True
        where.add_argument(p.flag, dest=p.name, **options)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="odcl", description="What data exists, how it connects, and which projects use it.")
    p.add_argument("--database-url", metavar="URL", help="the PostgreSQL database (default: $ODCL_DATABASE_URL)")
    p.add_argument("--as", dest="actor", help="who is registering, on a direct connection without $ODCL_TOKEN (default: $ODCL_USER, else your login name)")
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    sub = p.add_subparsers(dest="command", required=True)
    groups: dict[str, argparse._SubParsersAction] = {}
    for op in OPERATIONS:
        if not op.cli:
            continue
        group, _, command = op.cli.partition(" ")
        if command:
            if group not in groups:
                groups[group] = sub.add_parser(group, help=GROUPS[group]).add_subparsers(dest="what", required=True)
            s = groups[group].add_parser(command, help=op.help)
        else:
            s = sub.add_parser(group, help=op.help)
        _add_params(s, op)
        s.set_defaults(op=op.name)

    sub.add_parser("migrate", help="create or update the catalog's tables in the database")
    s = sub.add_parser("serve", help="serve the catalog over HTTP, with a web page to browse it")
    s.add_argument("--host", default="127.0.0.1", help="the address to listen on (default 127.0.0.1: only this machine)")
    s.add_argument("--require-sign-in", action="store_true", help="reads need an API token too (single sign-on, when set up, always needs a sign-in)")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--accept-lineage", action="store_true",
                   help="accept OpenLineage events at POST /api/v1/lineage; set ODCL_LINEAGE_TOKEN to require it as a bearer token")
    s = sub.add_parser("mcp", help="serve the catalog to an assistant over the Model Context Protocol")
    s.add_argument("--allow-writes", action="store_true", help="also offer the tools that register projects and record use")
    s.add_argument("--server", metavar="URL", help="use an odcl serve at URL instead of the database, signed in with $ODCL_TOKEN")
    return p


def _serve(args) -> int:
    from .server import make_server
    server = make_server(args.database_url, host=args.host, port=args.port, actor=args.actor, accept_lineage=args.accept_lineage,
                         require_sign_in=args.require_sign_in)
    host, port = server.server_address[:2]
    handler = server.RequestHandlerClass
    print(f"odcl: serving the catalog at http://{host}:{port}/" + ("  (accepting OpenLineage events at /api/v1/lineage)" if args.accept_lineage else "")
          + (f"\nodcl: single sign-on: {handler.sso.issuer}" if handler.sso else "\nodcl: reads need an API token" if handler.require_sign_in
             else "\nodcl: anyone who reaches this server can read the catalog; writes need an API token")
          + ("\nodcl: until the first user is made, anyone can send OpenLineage events; set ODCL_LINEAGE_TOKEN to require one"
             if args.accept_lineage and not os.environ.get("ODCL_LINEAGE_TOKEN") else ""), file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "migrate":
            applied = migrate(args.database_url)
            print(json.dumps({"applied": applied}, indent=2) if args.json else "\n".join([f"applied {name}" for name in applied] or ["The database is up to date."]))
            return 0
        if args.command == "serve":
            return _serve(args)
        token = os.environ.get("ODCL_TOKEN") or None
        if args.command == "mcp" and args.server:
            from .mcp import Remote
            from .mcp import main as serve
            return serve(Remote(args.server, token), args.allow_writes)
        with Catalog(args.database_url, args.actor, token=token) as catalog:
            if args.command == "mcp":
                from .mcp import main as serve
                return serve(catalog, args.allow_writes)
            op = operation(args.op)
            params = prepare(op, {p.name: getattr(args, p.name, None) for p in op.params})
            result = op.run(catalog, **params)
    except CatalogError as e:
        print(f"odcl: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str) if args.json else "\n".join(op.lines(result, params)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
