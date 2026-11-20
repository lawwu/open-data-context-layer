"""The `odcl` command (`fathom` is a temporary alias). Every subcommand calls one catalog operation and prints what it answers."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import timedelta

from .store import Catalog, CatalogError, migrate, require


def span(value: str) -> timedelta:
    """7d or 12h as a length of time."""
    found = re.fullmatch(r"(\d{1,4})([dh])", value or "")
    require(found, "since", f"{value!r} is not a span of time: write days or hours, like 7d or 12h")
    return timedelta(**{"days" if found.group(2) == "d" else "hours": int(found.group(1))})


def flag(entry) -> str:
    return "  (changed since)" if entry.get("changed_since") else ""


def _defined(defined: dict) -> str:
    return f"{defined['file']} at commit {defined['commit']}" if defined.get("commit") else f"{defined['file']} (no git commit)"


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
    if command == "import" and "model" in result:
        checked = ("already recorded, so the artifact was not read again" if result["artifact_verified"] == "recorded"
                   else f"artifact checked by {result['artifact_verified']}")
        return [f"{result['model']} {result['version']}  {result['change']}  ({result['inputs']} input(s), {checked})"]
    if command == "explain" and "model" in result:
        produced, artifact, read = result["produced_by"], result["artifact"], result["read"]
        out = [f"{result['model']} / {result['version']}",
               f"  produced by   {produced['pipeline_run']} / task {produced['task']}",
               f"  artifact      {artifact['uri']}#{artifact['generation']}  sha256 {artifact['sha256'][:12]}…",
               f"  read          {read['dataset_version']}, snapshot of {read['snapshot_of']}"]
        for i in result["inputs"]:
            out += [f"  {i['role']:<8} {i['field']} = {i['expression'] or '?'}" + ("" if i["lineage"] == "resolved" else f"  ({i['lineage']})"),
                    f"           built by {i['produced_by']['dataform_invocation']}" if i["produced_by"]["dataform_invocation"] else "           built by ?",
                    "           as " + (f"BigQuery job ({i['executed_as']['project']}, {i['executed_as']['location']}, {i['executed_as']['job_id']})"
                                        if i["executed_as"] else "an unknown job") + f", from {_defined(i['defined_in'])}",
                    f"           from {', '.join(i['derived_from']) or '?'}"]
        return out + [f"missing: {m}" for m in result["missing"]]
    if command == "import" and "entries" in result:
        counts = result["entries"]
        return ([f"{counts['added']} added, {counts['changed']} changed, {counts['unchanged']} unchanged from {result['compilation_result']}"]
                + (["Definitions not applied: a newer compilation of this repository was imported before"] if result["definitions"] != "applied" else [])
                + [f"  {a['target']}  {a['state']}" + (f"  job {a['job_id']}" if a["job_id"] else "") for a in result["actions"]])
    if command == "import":
        return [f"{result['added']} added, {result['changed']} changed, {result['unchanged']} unchanged"]
    if command == "handoff":
        return [f"{result['reference']}  {result['change']}"]
    if command == "explain":
        produced, job, defined = result["produced_by"], result["executed_as"], result["defined_in"]
        out = [result["reference"], f"  snapshot of   {result['snapshot_of']}",
               f"  produced by   {produced['dataform_invocation']} / {produced['target']}  {produced['state']}",
               "  executed as   " + (f"BigQuery job ({job['project']}, {job['location']}, {job['job_id']})  {job['statement_type']}" if job else "unknown"),
               f"  defined in    {_defined(defined)}"]
        for c in result["columns"]:
            out.append(f"  {c['field']} = {c['expression'] or '?'}" + (f"  from {', '.join(d['column'] for d in c['derived_from'])}" if c["derived_from"] else "")
                       + ("" if c["lineage"] == "resolved" else f"  ({c['lineage']})"))
        return out + [f"missing: {m}" for m in result["missing"]]
    if command == "sync":
        return [f"{result['jobs']} jobs read, {result['added']} new, from {result['since']} to {result['until']}"
                + (f" ({result['excluded']} monitoring jobs left out)" if result["excluded"] else "")
                + ("" if result["finished"] else f"\nNot recorded as finished: Google could not reach {', '.join(result['unreachable'])}")]
    if command == "history":
        totals = result["totals"]
        out = [f"{result['table']}: {totals['jobs']} job(s) since {result['since']}, {totals['billed_bytes']} bytes billed"]
        out += [f"  {j['created']}  {'+'.join(j['roles']):<10}  {j['statement_type'] or '?':<24}  {j['project']}/{j['location']}/{j['job_id']}"
                + (f"  failed: {j['error_reason']}" if j["error_reason"] else "") + ("" if j["state"] == "DONE" else f"  {j['state']}") for j in result["jobs"]]
        if result["more"]:
            out.append(f"  ... and {totals['jobs'] - len(result['jobs'])} more")
        for p in result.get("patterns", []):
            out += [f"run {p['executions']} time(s), last {p['last_run']}, e.g. {p['example_job']['job_id']}:", f"  {p['template']}"]
        if not result["synced"]:
            out.append("No job history has been synced yet: run `odcl sync bigquery-jobs --project PROJECT`")
        return out
    if command == "migrate":
        return [f"applied {name}" for name in result["applied"]] or ["The database is up to date."]
    if command == "import-sqlite":
        return [f"{result['entries']} entries, {result['revisions']} revisions and {result['links']} pinned links imported from {result['source']}"]
    if command in ("add", "use"):
        return [f"{result['key']}  {result['change']}  ({result['revision']})"]
    return [json.dumps(result, indent=2)]


def run(catalog: Catalog, args):
    if args.command == "import-sqlite":
        from .legacy import import_sqlite
        return import_sqlite(catalog, args.path)
    if args.command == "import":
        require(sum(map(bool, (args.dbt, args.bigquery, args.dataform, args.vertex_manifest))) == 1, "import",
                "Import from one place: --dbt PATH, --bigquery TABLE..., --dataform INVOCATION or --vertex-manifest PATH")
        if args.dataform:
            from . import dataform
            return dataform.import_invocation(catalog, args.dataform, client=dataform.client())
        if args.vertex_manifest:
            from . import vertex
            source = args.vertex_manifest
            manifest = vertex.read_manifest(source, vertex.client("us-central1") if source.startswith("gs://") else None)
            location = vertex.JOB.match(manifest.get("pipeline_run") or "")
            require(location, "manifest", "The manifest's pipeline_run is not a Vertex AI pipeline job name")
            return vertex.import_build(catalog, manifest, client=vertex.client(location.group(2)))
        if args.bigquery:
            from . import bigquery
            client = bigquery.client(args.project)
            try:
                return bigquery.import_tables(catalog, args.bigquery, client=client)
            finally:
                client.close()
        from .dbt import dbt_entries
        return catalog.apply(dbt_entries(args.dbt, args.select))
    if args.command == "sync":
        from . import bigquery
        client = bigquery.client(args.project)
        try:
            return bigquery.sync_jobs(catalog, args.project, client=client, backfill=span(args.backfill), sql=args.sql)
        finally:
            client.close()
    if args.command == "handoff":
        from . import bigquery, builds
        client = bigquery.client(args.project)
        try:
            facts = bigquery.snapshot(args.snapshot, client=client)
        finally:
            client.close()
        return builds.record_handoff(catalog, args.invocation, args.target, facts)
    if args.command == "explain" and args.what == "model":
        return catalog.explain_model(args.name, args.version)
    if args.command == "explain":
        from . import builds
        return builds.explain_dataset(catalog, args.reference)
    if args.command == "history":
        return catalog.table_history(args.key, span(args.since), args.patterns, args.limit)
    if args.command == "add":
        fields = {k: v for k, v in vars(args).items() if k in ("path", "table", "name", "description", "system", "layer", "owner", "type", "dtype",
                                                                 "definition", "repository", "status", "derived_from", "tags") and v not in (None, [])}
        return catalog.put(args.kind, fields)
    if args.command == "find":
        return catalog.find(" ".join(args.words), args.kind, args.layer, args.limit)
    if args.command == "show":
        return catalog.get(args.key)
    if args.command == "lineage":
        return catalog.lineage(args.key, args.direction)
    if args.command == "use":
        return catalog.use(args.project, args.keys, args.purpose)
    if args.command == "used-by":
        return catalog.used_by(args.key)
    if args.command == "uses":
        return catalog.uses(args.project, args.upstream, args.revision)
    return catalog.overview()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="odcl", description="What data exists, how it connects, and which projects use it.")
    p.add_argument("--database-url", metavar="URL", help="the PostgreSQL database (default: $ODCL_DATABASE_URL)")
    p.add_argument("--as", dest="actor", help="who is registering (default: $ODCL_USER, else your login name)")
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("overview", help="how much is registered, and what changed after a project used it")

    sub.add_parser("migrate", help="create or update the catalog's tables in the database")

    s = sub.add_parser("import-sqlite", help="bring a Fathom SQLite catalog into an empty database, once")
    s.add_argument("path", help="the old SQLite file, e.g. ~/.catalog/catalog.db; it is only read")

    s = sub.add_parser("import", help="read tables and columns from a dbt project or from BigQuery")
    s.add_argument("--dbt", metavar="PATH", help="a dbt target folder or its manifest.json")
    s.add_argument("--select", nargs="+", metavar="MODEL", help="with --dbt: only these models and what they are built from")
    s.add_argument("--bigquery", nargs="+", metavar="TABLE", help="BigQuery tables to read, as project.dataset.table")
    s.add_argument("--dataform", metavar="INVOCATION",
                   help="a Dataform workflow invocation, as projects/P/locations/L/repositories/R/workflowInvocations/I")
    s.add_argument("--vertex-manifest", metavar="PATH", help="a training manifest, as a local file or gs:// object, written by a Vertex AI training task")
    s.add_argument("--project", help="with --bigquery: the project the API calls are made from (default: your credentials' project)")

    s = sub.add_parser("handoff", help="record that a successful build handed on a snapshot of its output")
    s.add_argument("--invocation", required=True, help="the Dataform workflow invocation that built the table")
    s.add_argument("--target", required=True, help="the table the build wrote, as project.dataset.table")
    s.add_argument("--snapshot", required=True, help="the table snapshot taken of it, as project.dataset.table")
    s.add_argument("--project", help="the project BigQuery calls are made from (default: your credentials' project)")

    s = sub.add_parser("explain", help="where something came from, as recorded")
    kinds = s.add_subparsers(dest="what", required=True)
    e = kinds.add_parser("dataset", help="a dataset version: its build, job, definition and each field's inputs")
    e.add_argument("reference", help="project.dataset.snapshot, or project.dataset.snapshot@time")
    m = kinds.add_parser("model", help="a model version: its training task, artifact, data and each input's lineage")
    m.add_argument("name")
    m.add_argument("--version", required=True)

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

    s = sub.add_parser("sync", help="read new activity from a source into the catalog")
    kinds = s.add_subparsers(dest="what", required=True)
    b = kinds.add_parser("bigquery-jobs", help="the query jobs a BigQuery project ran since the last sync")
    b.add_argument("--project", required=True, help="the project the jobs ran in, which may differ from where the tables live")
    b.add_argument("--backfill", default="7d", metavar="SPAN", help="how far back the first sync reads (default: 7d)")
    b.add_argument("--sql", choices=["templates", "none"], default="templates",
                   help="templates keeps each query's shape without its literals; none does not read SQL at all")

    s = sub.add_parser("history", help="the query jobs that read or wrote something")
    kinds = s.add_subparsers(dest="what", required=True)
    h = kinds.add_parser("table")
    h.add_argument("key", help="a table key or its BigQuery path")
    h.add_argument("--since", default="7d", metavar="SPAN", help="how far back to look, like 7d or 12h (default: 7d)")
    h.add_argument("--patterns", action="store_true", help="also group the jobs by query shape")
    h.add_argument("--limit", type=int, default=100)

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
    s.add_argument("--revision", metavar="REV", help="what this older revision of the project had pinned")

    s = sub.add_parser("mcp", help="serve the catalog to an assistant over the Model Context Protocol")
    s.add_argument("--allow-writes", action="store_true", help="also offer the tools that register projects and record use")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "migrate":
            result = {"applied": migrate(args.database_url)}
            print(json.dumps(result, indent=2) if args.json else "\n".join(lines(args.command, result)))
            return 0
        with Catalog(args.database_url, args.actor) as catalog:
            if args.command == "mcp":
                from .mcp import main as serve
                return serve(catalog, args.allow_writes)
            result = run(catalog, args)
    except CatalogError as e:
        print(f"odcl: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2) if args.json else "\n".join(lines(args.command, result)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
