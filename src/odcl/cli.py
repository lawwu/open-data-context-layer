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


def moment(value: str, name: str):
    """A time as ISO 8601 (a date, or a date and time; UTC unless it says), or a span back from now such as 7d."""
    from datetime import datetime, timezone

    from . import store
    if re.fullmatch(r"\d{1,4}[dh]", value or ""):
        return store.now() - span(value)
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise CatalogError(name, f"{value!r} is not a time: write 2026-10-05T09:00:00Z, 2026-10-05, or a span back from now like 12h") from None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def flag(entry) -> str:
    return "  (changed since)" if entry.get("changed_since") else ""


def _count(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _defined(defined: dict) -> str:
    return f"{defined['file']} at commit {defined['commit']}" if defined.get("commit") else f"{defined['file']} (no git commit)"


def _relied(documents: list[dict]) -> list[str]:
    return [f"  {d['document']} at {d['revision']} ({d['pinned_by'].replace('_', ' ')})" + (f"  for: {d['why']}" if d["why"] else "")
            + ("  (corrected since)" if d["changed_since"] else "") for d in documents]


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
    if command == "import" and "skipped" in result:
        c = result["documents"]
        out = [f"{c['added'] + c['changed'] + c['unchanged']} documents: {c['added']} added, {c['changed']} changed, {c['unchanged']} unchanged, "
               f"{c['retired']} retired; {result['skipped']} files without an odcl block skipped"]
        for key, items in result["unresolved"].items():
            entries = [i for i in items if not i.startswith("section ")]
            sections = [i.removeprefix("section ") for i in items if i.startswith("section ")]
            if entries:
                out.append(f"  {key}  names {len(entries)} {'entry' if len(entries) == 1 else 'entries'} the catalog does not have: {', '.join(entries)}")
            if sections:
                out.append(f"  {key}  names a section the page has no heading for: {', '.join(sections)}")
        return out
    if command == "docs":
        return [f"{d['document']}" + (f"  #{d['section']}" if d["section"] else "") + (f"  {d['says']}" if d["says"] else "")
                + (f"  (about {d['through']})" if d["through"] != result["key"] else "") + ("  (redefined since the page described it)" if d["changed_since"] else "")
                for d in result["documents"]] or ["No document describes this."]
    if command == "import" and "events" in result:
        events = result["events"]
        recorded = sum(e["change"] == "recorded" for e in events)
        runs = {e["run"]: e for e in events if e["run"]}  # each run once, as it stands after its last event
        return ([f"{len(events)} events: {recorded} recorded, {len(events) - recorded} duplicate"]
                + [f"  {e['job']}  {e['state']}" for e in runs.values()])
    if command == "import" and "model" in result:
        checked = ("already recorded, so the artifact was not read again" if result["artifact_verified"] == "recorded"
                   else f"artifact checked by {result['artifact_verified']}")
        return [f"{result['model']} {result['version']}  {result['change']}  ({result['inputs']} input(s), {checked})"]
    if command == "record":
        return [f"{result['reference']}  {result['change']}  ({_count(result['objects'], 'object')}, {_count(result['fields'], 'field')})"] + (
            [f"fields not known: {result['schema']}"] if result["schema"] != "read" else []) + (
            [f"exported by {', '.join(result['exported_by'])}"] if result.get("exported_by") else [])
    if command == "explain" and "model" in result and result["read"].get("kind") == "gcs_files":
        produced, artifact, read = result["produced_by"], result["artifact"], result["read"]
        out = [f"{result['model']} / {result['version']}",
               f"  produced by   {produced['pipeline_run']} / task {produced['task']}",
               f"  artifact      {artifact['uri']}#{artifact['generation']}  sha256 {artifact['sha256'][:12]}…",
               f"  read          {read['uri']}  ({_count(read['objects'], 'object')}, generations pinned)",
               "  written by    " + (f"{read['produced_by']['pipeline_run']} / task {read['produced_by']['task']}" if read["produced_by"] else "unknown"),
               "  exported from " + (f"{read['exported_from']['table']}" + ("  (changed since)" if read["exported_from"]["changed_since"] else "")
                                     if read["exported_from"] else "not recorded")]
        for j in read.get("exported_by") or []:
            out.append(f"  exported by   BigQuery job ({j['job']})  {j['statement_type']}" + (f"  {j['rows']:,} rows" if j["rows"] is not None else ""))
        if read.get("row_filter"):
            out.append(f"  row filter    {read['row_filter']['expression'] or ', '.join(read['row_filter']['columns'])}")
        out += [f"  {i['role']:<8} {i['field']} = {i['expression'] or '?'}  from {', '.join(i['derived_from']) or '?'}" if "derived_from" in i
                else f"  {i['role']:<8} {i['field']}  {i['dtype'] or '?'}  (lineage not claimed)" for i in result["inputs"]]
        out += (["  relied on"] + _relied(result["relied_on"])) if result.get("relied_on") else []
        return out + [f"missing: {m}" for m in result["missing"]]
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
        out += (["  relied on"] + _relied(result["relied_on"])) if result.get("relied_on") else []
        return out + [f"missing: {m}" for m in result["missing"]]
    if command == "import" and "entries" in result:
        counts = result["entries"]
        return ([f"{counts['added']} added, {counts['changed']} changed, {counts['unchanged']} unchanged from {result['compilation_result']}"]
                + (["Definitions not applied: a newer compilation of this repository was imported before"] if result["definitions"] != "applied" else [])
                + [f"  {a['target']}  {a['state']}" + (f"  job {a['job_id']}" if a["job_id"] else "") for a in result["actions"]])
    if command == "import" and "definitions" in result:
        return [f"{result['definitions']} definitions: {result['added']} entries added, {result['changed']} changed, {result['unchanged']} unchanged; "
                f"{result['columns_traced']} columns traced, {result['columns_partial']} in part"]
    if command == "import":
        return [f"{result['added']} added, {result['changed']} changed, {result['unchanged']} unchanged"]
    if command == "handoff":
        return [f"{result['reference']}  {result['change']}"]
    if command == "explain" and result.get("kind") == "gcs_files":
        produced, exported = result["produced_by"], result["exported_from"]
        out = [result["reference"], f"  files         {result['uri']}  ({_count(len(result['objects']), 'object')}, generations pinned)",
               "  written by    " + (f"{produced['pipeline_run']} / task {produced['task']}  {produced['state']}" if produced else "unknown"),
               "  exported from " + (f"{exported['table']}" + ("  (changed since)" if exported["changed_since"] else "") if exported else "not recorded")]
        for j in result.get("exported_by") or []:
            out.append(f"  exported by   BigQuery job ({j['job']})  {j['statement_type']}" + (f"  {j['rows']:,} rows" if j["rows"] is not None else ""))
        if result.get("row_filter"):
            out.append(f"  row filter    {result['row_filter']['expression'] or ', '.join(result['row_filter']['columns'])}")
        out += [f"  {c['field']} = {c.get('expression') or '?'}" + (f"  from {', '.join(d['column'] for d in c['derived_from'])}" if c["derived_from"] else "")
                + ("" if c["lineage"] == "resolved" else f"  ({c['lineage']})") if "derived_from" in c else f"  {c['field']}  {c['dtype'] or '?'}"
                for c in result["columns"]]
        return out + [f"missing: {m}" for m in result["missing"]]
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
                + (" (this window only: the next sync starts where the last one ended)" if result.get("window") == "explicit" else "")
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
    if command == "privacy" and "entries" in result:
        return [f"{e['key']}  {e['change']}  ({e['revision']})" for e in result["entries"]]
    if command == "privacy":
        import csv
        import io
        out = io.StringIO()
        fields = ["key", "table", "column", "dtype", "pii", "scrubbing", "data_category", "retention", "owner", "table_pii", "inherited"]
        writer = csv.DictWriter(out, fields, lineterminator="\n")
        writer.writeheader()
        for c in result["columns"]:
            writer.writerow({**c, **{f: {True: "yes", False: "no", None: ""}[c[f]] for f in ("pii", "table_pii")}, "inherited": " ".join(c["inherited"])})
        return [out.getvalue().rstrip("\n")]
    if command == "migrate":
        return [f"applied {name}" for name in result["applied"]] or ["The database is up to date."]
    if command == "import-sqlite":
        return [f"{result['entries']} entries, {result['revisions']} revisions and {result['links']} pinned links imported from {result['source']}"]
    if command == "discover":
        return [f"{r['created'] or '?'}  {r['state'] or '?':<26}  {r['pipeline_run']}" + (f"  {r['display_name']}" if r["display_name"] else "")
                + (f"\n    recorded: {', '.join(r['model_versions'])}" if r["model_versions"] else "") for r in result["runs"]] or ["No pipeline run in that window."]
    if command == "propose":
        out = [json.dumps(result["manifest"], indent=2), "", "Where each value came from:"]
        out += [f"  {k}: {v}" for k, v in result["sources"].items()]
        out += ["", "Ready to import." if result["ready"] else "Review before importing:"] + [f"  - {r}" for r in result["review"]]
        out += [f"  missing: {', '.join(result['missing'])}"] if result["missing"] else []
        return out + ([f"\nWritten to {result['written_to']}: odcl import --vertex-manifest {result['written_to']}"] if result["written_to"] else [])
    if command == "rely":
        return [f"{result['model']} {result['version']} relied on:"] + _relied(result["relied_on"])
    if command == "instances":
        copies = [f"  copy: {link['to']}" for link in result["links"] if link["relation"] == "instance_of"]
        return [f"{result['key']}  {result['change']}  ({result['revision']})"] + (copies or ["  no table matches yet"])
    if command in ("add", "use"):
        return [f"{result['key']}  {result['change']}  ({result['revision']})"]
    return [json.dumps(result, indent=2)]


def run(catalog: Catalog, args):
    if args.command == "import-sqlite":
        from .legacy import import_sqlite
        return import_sqlite(catalog, args.path)
    if args.command == "import":
        require(sum(map(bool, (args.dbt, args.bigquery, args.dataform, args.vertex_manifest, args.openlineage, args.docs, args.sql_definitions))) == 1,
                "import", "Import from one place: --dbt PATH, --bigquery TABLE..., --dataform INVOCATION, --vertex-manifest PATH, --openlineage FILE, "
                          "--docs PATH... or --sql-definitions FILE")
        if args.sql_definitions:
            from .sqldefs import import_definitions
            return import_definitions(catalog, args.sql_definitions, dialect=args.dialect)
        if args.docs:
            from .docs import import_docs
            return import_docs(catalog, args.docs, repo_url=args.repo_url, commit=args.commit)
        if args.openlineage:
            from . import openlineage
            return {"events": [openlineage.ingest(catalog, e) for e in openlineage.read_events(args.openlineage)]}
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
        since = moment(args.since, "since") if args.since else None
        until = moment(args.until, "until") if args.until else None
        require(not args.task or args.pipeline_run, "task", "--task names a task of the --pipeline-run")
        if args.pipeline_run:
            from . import vertex
            require(not (since or until), "since", "Give --pipeline-run or --since/--until, not both")
            location = vertex.JOB.match(args.pipeline_run)
            require(location, "pipeline_run", "--pipeline-run is a Vertex AI pipeline job: projects/P/locations/L/pipelineJobs/J")
            since, until = vertex.run_window(vertex.client(location.group(2)).get(args.pipeline_run), args.pipeline_run, args.task)
        client = bigquery.client(args.project)
        try:
            return bigquery.sync_jobs(catalog, args.project, client=client, since=since, until=until, backfill=span(args.backfill), sql=args.sql)
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
    if args.command == "record":
        from . import filesets, vertex
        location = vertex.JOB.match(args.pipeline_run or "")
        require(location or not args.pipeline_run, "files", "--pipeline-run is a Vertex AI pipeline job: projects/P/locations/L/pipelineJobs/J")
        return filesets.record(catalog, client=vertex.client(location.group(2) if location else "us-central1"), pipeline_run=args.pipeline_run,
                               task=args.task, output=args.output, uris=args.uri, from_table=args.from_table, export_jobs=args.export_job,
                               find_export_job=args.find_export_job, sql=args.sql)
    if args.command == "explain" and args.what == "model":
        return catalog.explain_model(args.name, args.version)
    if args.command == "explain":
        from . import builds
        return builds.explain_dataset(catalog, args.reference)
    if args.command == "history":
        return catalog.table_history(args.key, span(args.since), args.patterns, args.limit)
    if args.command == "discover":
        from . import discover, vertex
        return {"runs": discover.runs(catalog, vertex.client(args.location), args.project, moment(args.since, "since"))}
    if args.command == "propose":
        from . import discover, vertex
        location = vertex.JOB.match(args.pipeline_run or "")
        require(location, "pipeline_run", "--pipeline-run is a Vertex AI pipeline job: projects/P/locations/L/pipelineJobs/J")
        found = discover.propose(catalog, vertex.client(location.group(2)), args.pipeline_run, trainer=args.trainer, loader=args.loader,
                                 loader_output=args.loader_output, model=args.model, version=args.version)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(found["manifest"], f, indent=2)
                f.write("\n")
        return {**found, "written_to": args.out}
    if args.command == "rely":
        return catalog.rely(args.model, args.version, args.documents, args.why)
    if args.command == "instances":
        return catalog.set_instances(args.definition, args.patterns)
    if args.command == "privacy" and args.what == "set":
        return {"entries": catalog.set_privacy(args.keys, pii=args.pii, data_category=args.data_category, retention=args.retention, scrubbing=args.scrubbing)}
    if args.command == "privacy":
        return {"columns": catalog.privacy(args.pii_only, args.table)}
    if args.command == "add":
        fields = {k: v for k, v in vars(args).items() if k in ("path", "table", "name", "description", "system", "layer", "owner", "type", "dtype",
                                                                 "definition", "repository", "status", "derived_from", "tags", "pii", "data_category",
                                                                 "retention", "scrubbing") and v not in (None, [])}
        return catalog.put(args.kind, fields)
    if args.command == "find":
        return catalog.find(" ".join(args.words), args.kind, args.layer, args.limit)
    if args.command == "docs":
        from .store import DESCRIBED
        return {"key": catalog.resolve(args.key, DESCRIBED), "documents": catalog.documents(args.key)}
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


def privacy_flags(p) -> None:
    p.add_argument("--pii", choices=["yes", "no"], help="whether it holds personal data")
    p.add_argument("--data-category", metavar="CATEGORY", help="e.g. contact, financial, health; a column without one takes its table's")
    p.add_argument("--retention", metavar="PERIOD", help="how long it is kept, e.g. 400d or 7 years; a column without one takes its table's")
    p.add_argument("--scrubbing", metavar="HOW", help="how the personal data is scrubbed, e.g. hashed, truncated, removed")


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
    s.add_argument("--docs", nargs="+", metavar="PATH", help="Markdown pages, or folders of them, whose frontmatter has an odcl block")
    s.add_argument("--repo-url", metavar="URL", help="with --docs: the repository's web address, when git cannot say")
    s.add_argument("--commit", metavar="SHA", help="with --docs: the commit the pages were read at, when git cannot say")
    s.add_argument("--openlineage", metavar="FILE", help="OpenLineage events: one JSON event, a JSON list of them, or JSON Lines")
    s.add_argument("--sql-definitions", metavar="FILE", help="JSON Lines of tables and the SELECT that builds each: target, select, dependencies, "
                                                             "description, columns, type, file (or declaration: true)")
    s.add_argument("--dialect", default="bigquery", help="with --sql-definitions: the SQL dialect, as sqlglot names it (default: bigquery)")
    s.add_argument("--project", help="with --bigquery: the project the API calls are made from (default: your credentials' project)")

    s = sub.add_parser("handoff", help="record that a successful build handed on a snapshot of its output")
    s.add_argument("--invocation", required=True, help="the Dataform workflow invocation that built the table")
    s.add_argument("--target", required=True, help="the table the build wrote, as project.dataset.table")
    s.add_argument("--snapshot", required=True, help="the table snapshot taken of it, as project.dataset.table")
    s.add_argument("--project", help="the project BigQuery calls are made from (default: your credentials' project)")

    s = sub.add_parser("record", help="record a dataset version that is not a BigQuery snapshot")
    kinds = s.add_subparsers(dest="what", required=True)
    f = kinds.add_parser("dataset-files", help="the Cloud Storage files a training run read, such as a data-loader task's Parquet output")
    f.add_argument("--pipeline-run", metavar="JOB", help="the Vertex AI pipeline job whose task wrote the files")
    f.add_argument("--task", help="with --pipeline-run: the task that wrote them")
    f.add_argument("--output", metavar="NAME", help="with --pipeline-run: the task's output artifact, e.g. output_train_dataset")
    f.add_argument("--uri", nargs="+", metavar="GS_URI", help="instead: gs:// files or folders not written by a pipeline task")
    f.add_argument("--from-table", metavar="TABLE", help="the catalog table the files were exported from (table-level lineage only)")
    f.add_argument("--export-job", action="append", metavar="PROJECT:LOCATION.JOB_ID",
                   help="the BigQuery EXPORT DATA job that wrote the files; each field is traced through its SELECT (repeat for several)")
    f.add_argument("--find-export-job", action="store_true", help="with --pipeline-run: find the EXPORT DATA job among the jobs created while the task ran")
    f.add_argument("--sql", choices=["templates", "none"], default="templates", help="keep the export job's SQL as its shape (default), or not at all")

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
        privacy_flags(k)

    s = sub.add_parser("discover", help="find runs whose model versions the catalog has not recorded")
    kinds = s.add_subparsers(dest="what", required=True)
    d = kinds.add_parser("vertex-runs", help="the Vertex AI pipeline runs of a project, and which already have a model version recorded")
    d.add_argument("--project", required=True)
    d.add_argument("--location", default="us-central1")
    d.add_argument("--since", default="7d", metavar="TIME", help="runs created since this time or span (default: 7d)")

    s = sub.add_parser("propose", help="draft something for a person to review before it is imported")
    kinds = s.add_subparsers(dest="what", required=True)
    d = kinds.add_parser("vertex-manifest", help="a training manifest for a pipeline run, from its trainer's metadata.json and its loader's output")
    d.add_argument("--pipeline-run", required=True, metavar="JOB")
    d.add_argument("--trainer", metavar="TASK", help="the task that trained the model (default: the one with a Model output)")
    d.add_argument("--loader", metavar="TASK", help="the task that wrote the training data (default: the one with a Dataset output)")
    d.add_argument("--loader-output", metavar="NAME", help="the loader's output the model read (default: the one named for training)")
    d.add_argument("--model", help="the model's name, when metadata.json does not say it")
    d.add_argument("--version", help="the model version, when metadata.json does not say it (default: the pipeline job's id)")
    d.add_argument("--out", metavar="FILE", help="also write the draft manifest here, to review and then import with --vertex-manifest")

    s = sub.add_parser("rely", help="record that a model version relied on documents, such as a finding, at the revision it was trained on")
    s.add_argument("model")
    s.add_argument("--version", required=True)
    s.add_argument("documents", nargs="+", metavar="DOCUMENT")
    s.add_argument("--why", help="what the model took from them")

    s = sub.add_parser("instances", help="say which tables are copies of a definition table, by patterns over their paths")
    s.add_argument("definition", help="the definition table, e.g. core.sales.daily_totals")
    s.add_argument("patterns", nargs="*", metavar="PATTERN", help="globs over table paths, e.g. 't-*.sales.daily_totals'; none clears them")

    s = sub.add_parser("privacy", help="which tables and columns hold personal data, their data category and retention")
    kinds = s.add_subparsers(dest="what", required=True)
    v = kinds.add_parser("set", help="say these for tables or columns, keeping what else they say")
    v.add_argument("keys", nargs="+", metavar="KEY")
    privacy_flags(v)
    v = kinds.add_parser("export", help="every column with its privacy fields, as CSV (or --json)")
    v.add_argument("--pii-only", action="store_true", help="only columns marked as personal data")
    v.add_argument("--table", help="only this table's columns")

    s = sub.add_parser("sync", help="read new activity from a source into the catalog")
    kinds = s.add_subparsers(dest="what", required=True)
    b = kinds.add_parser("bigquery-jobs", help="the query jobs a BigQuery project ran since the last sync")
    b.add_argument("--project", required=True, help="the project the jobs ran in, which may differ from where the tables live")
    b.add_argument("--backfill", default="7d", metavar="SPAN", help="how far back the first sync reads (default: 7d)")
    b.add_argument("--since", metavar="TIME", help="read exactly the jobs created from this time (ISO 8601, or a span back from now like 12h); "
                                                   "the regular sync still starts where it last ended")
    b.add_argument("--until", metavar="TIME", help="read the jobs created up to this time (default: now)")
    b.add_argument("--pipeline-run", metavar="JOB", help="read the jobs created while this Vertex AI pipeline job ran")
    b.add_argument("--task", help="with --pipeline-run: only while this task of it ran")
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
    s.add_argument("--kind", choices=["table", "column", "project", "document"])
    s.add_argument("--layer")
    s.add_argument("--limit", type=int, default=50)

    s = sub.add_parser("docs", help="the documents that describe a table, column or project")
    s.add_argument("key")

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

    s = sub.add_parser("serve", help="serve the catalog over HTTP, with a web page to browse it")
    s.add_argument("--host", default="127.0.0.1", help="the address to listen on (default 127.0.0.1: only this machine). There is no sign-in")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--accept-lineage", action="store_true",
                   help="accept OpenLineage events at POST /api/v1/lineage; set ODCL_LINEAGE_TOKEN to require it as a bearer token")

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
        if args.command == "serve":
            from .server import make_server
            server = make_server(args.database_url, host=args.host, port=args.port, actor=args.actor, accept_lineage=args.accept_lineage)
            host, port = server.server_address[:2]
            print(f"odcl: serving the catalog at http://{host}:{port}/" + ("  (accepting OpenLineage events at /api/v1/lineage)" if args.accept_lineage else ""),
                  file=sys.stderr, flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
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
