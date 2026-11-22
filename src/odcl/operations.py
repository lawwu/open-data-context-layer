"""Every operation odcl offers, once: what it takes, what it does, and how each way in names it.

The command line (`odcl.cli`), the HTTP API (`odcl.server`) and the MCP tools (`odcl.mcp`) are adapters over this
table. Each `Operation` says what it runs, the `Param`s it takes, and where it appears: a command, an HTTP route,
an MCP tool, any of them. Each `Param` says how each adapter spells it: a positional argument or a flag, a query or
body field, an MCP argument. A new operation is one entry here; the adapters find it.

`run(catalog, **params)` does the work and returns plain data: what `--json`, the HTTP API and the MCP tools return.
`text(result, params)` is what a person reads on the command line. `role` is what the HTTP API requires of the
caller before running; the functions behind it check their own rights as well, which is what guards a direct
database connection.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .store import CatalogError, require
from .times import moment, span

ROLES = ("anyone", "reader", "editor", "importer", "admin")


@dataclass(frozen=True)
class Param:
    """One input of an operation.

    `kind`: str, int, bool, list, words (a list of words on the command line, one string elsewhere), or any (JSON).
    `cli`: "" for a flag named after the param, "positional", an explicit flag such as "--for", or None for none.
    `http`: the query or body field's name ("" for the param's own), or None for none; a {name} in the route's path
    takes the param of that name. `mcp`: the MCP argument's name, or None. `group`: command-line flags that exclude
    each other; a name ending in ! means one of them is required."""
    name: str
    help: str = ""
    kind: str = "str"
    required: bool = False
    default: Any = None
    choices: tuple | None = None
    cli: str | None = ""
    nargs: str | None = None
    metavar: str | None = None
    append: bool = False
    group: str | None = None
    http: str | None = ""
    mcp: str | None = None
    mcp_help: str | None = None
    mcp_schema: dict = field(default_factory=dict)

    @property
    def flag(self) -> str:
        return self.cli or "--" + self.name.replace("_", "-")

    @property
    def field(self) -> str:
        return self.http or self.name


@dataclass(frozen=True)
class Operation:
    name: str
    help: str
    run: Callable[..., Any]
    params: tuple[Param, ...] = ()
    role: str = "reader"
    cli: str | None = None      # "alerts subscribe": a command, or a group and a command
    http: str | None = None     # "GET /v1/entries/{key}/diff"
    mcp: str | None = None      # the tool's name
    mcp_help: str | None = None
    mcp_wrap: str | None = None  # the key an MCP result is put under, for a result that is a list
    text: Callable[[Any, dict], list[str]] | None = None
    remote: Callable[[dict], tuple[str, dict]] | None = None  # through a server: the operation and params to send, worked out here


    def lines(self, result, params: dict) -> list[str]:
        return self.text(result, params) if self.text else [json.dumps(result, indent=2, default=str)]


def through_server(remote, op: Operation, params: dict):
    """Run an operation through an `odcl serve` (`odcl.mcp.Remote`) rather than the database. An operation whose
    params need working out on this machine first, such as a local file read from git, says how in `remote`. One
    that reads from somewhere only this machine reaches (an import, a sync) cannot go through a server."""
    sent, what = op, params
    if op.remote:
        name, what = op.remote(params)
        sent = BY_NAME[name]
    require(sent.http, "server", f"odcl {op.cli} reads from where the data is, so it needs a database connection, not --server")
    left = [p.name.upper() if p.cli == "positional" else p.flag for p in sent.params
            if p.http is None and p.cli is not None and what.get(p.name) not in (None, False, [], p.default)]
    require(not left, "server", f"{', '.join(left)} cannot go through a server")
    return remote.call(sent, what)


def prepare(op: Operation, given: dict) -> dict:
    """The keyword arguments for `op.run` from what an adapter read, by param name: defaults filled in, words
    joined, and strings that say an int or a bool turned into one."""
    out = {}
    for p in op.params:
        value = given.get(p.name)
        if value is None or (p.kind == "list" and value == [] and p.default is not None):
            value = p.default
        if p.kind == "words" and isinstance(value, list):
            value = " ".join(value)
        elif p.kind == "int" and isinstance(value, str):
            require(value.isdigit(), p.name, f"{p.name} is a whole number")
            value = int(value)
        elif p.kind == "bool" and isinstance(value, str):
            value = value.lower() in ("1", "true", "yes")
        if p.choices and value is not None and not isinstance(value, bool):
            require(value in p.choices, p.name, f"{p.name} is one of: {', '.join(map(str, p.choices))}")
        out[p.name] = value
    return out


# ---- small things the text views share -------------------------------------------------------------------------
def short(key: str) -> str:
    return key.split(":", 1)[-1]


def _count(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _flag(entry) -> str:
    return "  (changed since)" if entry.get("changed_since") else ""


def _defined(defined: dict) -> str:
    return f"{defined['file']} at commit {defined['commit']}" if defined.get("commit") else f"{defined['file']} (no git commit)"


def _created(c: dict | None) -> str:
    if not c:
        return "unknown"
    where = c.get("path") or c.get("url") or c.get("command") or ""
    return (f"{c['kind']} {where}" + (f" at {c['commit'][:12]}" if c.get("commit") else "") + (" (with uncommitted changes)" if c.get("uncommitted") else "")
            + (f" in {c['repository']}" if c.get("repository") else "") + "  (as stated)")


def _relied(documents: list[dict]) -> list[str]:
    return [f"  {d['document']} at {d['revision']} ({d['pinned_by'].replace('_', ' ')})" + (f"  for: {d['why']}" if d["why"] else "")
            + ("  (corrected since)" if d["changed_since"] else "") for d in documents]


def _was(change: dict) -> str:
    """Before -> after, for a field whose values are short enough to read on one line."""
    if change.get("added") is not None:
        return "".join([f"  +{', '.join(map(short, change['added']))}" if change["added"] else "",
                        f"  -{', '.join(map(short, change['removed']))}" if change["removed"] else ""])
    before, after = change.get("before"), change.get("after")
    if change["field"] is None or len(f"{before}{after}") > 120:
        return ""
    return f": {before if before not in (None, '') else '(none)'} -> {after if after not in (None, '') else '(none)'}"


def _revision(result, params=None) -> list[str]:
    return [f"{result['key']}  {result['change']}  ({result['revision']})"]


def _user(u: dict) -> str:
    return (f"{u['name']}  {u['role']}" + (f"  {u['display_name']}" if u.get("display_name") else "") + (f"  <{u['email']}>" if u.get("email") else "")
            + ("  single sign-on" if u.get("single_sign_on") else "") + ("  DISABLED" if u.get("disabled") else "")
            + (f"  {_count(u['tokens'], 'token')}" if "tokens" in u else ""))


# ---- what each operation does, where it needs more than one call ----------------------------------------------
def _module(name: str):
    from importlib import import_module
    return import_module(f"odcl.{name}")


def _put(kind: str, merge: bool = False, **fields):
    def run(catalog, **given):
        return catalog.put(kind, {**fields, **{k: v for k, v in given.items() if v not in (None, [])}}, merge=merge)
    return run


def _subscribe(catalog, project=None, owner=None, all=False, channel=None, address=None, min_severity="semantic", webhook=None, slack=None, email=None):  # noqa: A002
    alerts = _module("alerts")
    said = [(c, v) for c, v in (("webhook", webhook), ("slack", slack), ("email", email)) if v]
    if said:  # the command line names the channel by its flag
        channel, address = said[0]
    require(channel, "channel", f"Send alerts somewhere: a channel ({', '.join(alerts.CHANNELS)}) and its address")
    return alerts.subscribe(catalog, project=project, owner=owner, everything=bool(all), channel=channel, address=address or "", min_severity=min_severity)


def _set_user(catalog, name, role=None, disabled=None, disable=False, enable=False):
    if disabled is None:
        disabled = True if disable else False if enable else None
    return _module("users").update(catalog, name, role, disabled)


def _created_by(catalog, key, what=None, kind=None, command_line=None, note=None, repo_url=None, commit=None, clear=False, created_by=None):
    if created_by is None and not clear and (what or command_line or note):
        created_by = _module("provenance").describe(what, kind=kind, command=command_line, note=note, repo_url=repo_url, commit=commit)
    return catalog.set_created_by(key, None if clear else created_by)


def _created_by_remote(p: dict) -> tuple[str, dict]:
    said = p.get("created_by")
    if said is None and not p.get("clear") and (p.get("what") or p.get("command_line") or p.get("note")):
        said = _module("provenance").describe(p.get("what"), kind=p.get("kind"), command=p.get("command_line"), note=p.get("note"),
                                              repo_url=p.get("repo_url"), commit=p.get("commit"))
    return "created_by", {"key": p["key"], "created_by": None if p.get("clear") else said}


def _subscribe_remote(p: dict) -> tuple[str, dict]:
    said = [(c, p.get(c)) for c in ("webhook", "slack", "email") if p.get(c)]
    channel, address = said[0] if said else (p.get("channel"), p.get("address"))
    return "alerts_subscribe", {**{k: v for k, v in p.items() if k not in ("webhook", "slack", "email")}, "channel": channel, "address": address}


def _proposals(catalog, table=None, all=False, min_confidence="low", write=None):  # noqa: A002
    pii = _module("pii")
    found = pii.propose(catalog, table, bool(all), min_confidence)
    return {"proposals": found, **({"written_to": pii.write_review(found, write)} if write else {})}


def _popular(catalog, since="30d", limit=50):
    return {"since": since, "tables": _module("popularity").popular(catalog, span(since), limit)}


def _history(catalog, table, since="7d", patterns=False, limit=100):
    return catalog.table_history(table, span(since), bool(patterns), limit)


IMPORT_FROM = ("dbt", "bigquery", "postgres", "dataform", "vertex_manifest", "openlineage", "docs", "sql_definitions", "quality", "dbt_tests")


def _import(catalog, **a):
    require(sum(map(bool, (a[k] for k in IMPORT_FROM))) == 1,
            "import", "Import from one place: --dbt PATH, --bigquery TABLE..., --postgres URL, --dataform INVOCATION, --vertex-manifest PATH, "
                      "--openlineage FILE, --docs PATH..., --sql-definitions FILE, --quality FILE or --dbt-tests PATH")
    if a["quality"] or a["dbt_tests"]:
        quality = _module("quality")
        return quality.record(catalog, quality.read_jsonl(a["quality"]) if a["quality"] else quality.dbt_results(a["dbt_tests"]),
                              a["source"] or ("dbt" if a["dbt_tests"] else "quality"))
    if a["postgres"]:
        return _module("postgres").import_database(catalog, a["postgres"], schemas=a["schema"], database=a["database_name"])
    if a["sql_definitions"]:
        return _module("sqldefs").import_definitions(catalog, a["sql_definitions"], dialect=a["dialect"], system=a["system"])
    if a["docs"]:
        return _module("docs").import_docs(catalog, a["docs"], repo_url=a["repo_url"], commit=a["commit"])
    if a["openlineage"]:
        openlineage = _module("openlineage")
        return {"events": [openlineage.ingest(catalog, e) for e in openlineage.read_events(a["openlineage"])]}
    if a["dataform"]:
        dataform = _module("dataform")
        return dataform.import_invocation(catalog, a["dataform"], client=dataform.client())
    if a["vertex_manifest"]:
        vertex = _module("vertex")
        manifest = vertex.read_manifest(a["vertex_manifest"], vertex.client("us-central1") if a["vertex_manifest"].startswith("gs://") else None)
        location = vertex.JOB.match(manifest.get("pipeline_run") or "")
        require(location, "manifest", "The manifest's pipeline_run is not a Vertex AI pipeline job name")
        return vertex.import_build(catalog, manifest, client=vertex.client(location.group(2)))
    if a["bigquery"]:
        bigquery = _module("bigquery")
        client = bigquery.client(a["project"])
        try:
            return bigquery.import_tables(catalog, a["bigquery"], client=client)
        finally:
            client.close()
    return _module("dbt").import_project(catalog, a["dbt"], a["select"])


def _sync(catalog, project, backfill="7d", since=None, until=None, pipeline_run=None, task=None, sql="templates"):
    bigquery = _module("bigquery")
    since = moment(since, "since") if since else None
    until = moment(until, "until") if until else None
    require(not task or pipeline_run, "task", "--task names a task of the --pipeline-run")
    if pipeline_run:
        vertex = _module("vertex")
        require(not (since or until), "since", "Give --pipeline-run or --since/--until, not both")
        location = vertex.JOB.match(pipeline_run)
        require(location, "pipeline_run", "--pipeline-run is a Vertex AI pipeline job: projects/P/locations/L/pipelineJobs/J")
        since, until = vertex.run_window(vertex.client(location.group(2)).get(pipeline_run), pipeline_run, task)
    client = bigquery.client(project)
    try:
        return bigquery.sync_jobs(catalog, project, client=client, since=since, until=until, backfill=span(backfill), sql=sql)
    finally:
        client.close()


def _handoff(catalog, invocation, target, snapshot, project=None):
    bigquery = _module("bigquery")
    client = bigquery.client(project)
    try:
        facts = bigquery.snapshot(snapshot, client=client)
    finally:
        client.close()
    return _module("builds").record_handoff(catalog, invocation, target, facts)


def _record_files(catalog, pipeline_run=None, task=None, output=None, uri=None, from_table=None, export_job=None, find_export_job=False, sql="templates",
                  created_by=None, created_by_note=None):
    vertex = _module("vertex")
    location = vertex.JOB.match(pipeline_run or "")
    require(location or not pipeline_run, "files", "--pipeline-run is a Vertex AI pipeline job: projects/P/locations/L/pipelineJobs/J")
    created = _module("provenance").describe(created_by, note=created_by_note) if created_by or created_by_note else None
    return _module("filesets").record(catalog, client=vertex.client(location.group(2) if location else "us-central1"), pipeline_run=pipeline_run,
                                      task=task, output=output, uris=uri, from_table=from_table, export_jobs=export_job,
                                      find_export_job=bool(find_export_job), sql=sql, created_by=created)


def _discover(catalog, project, location="us-central1", since="7d"):
    return {"runs": _module("discover").runs(catalog, _module("vertex").client(location), project, moment(since, "since"))}


def _propose_manifest(catalog, pipeline_run, trainer=None, loader=None, loader_output=None, model=None, version=None, out=None):
    vertex = _module("vertex")
    location = vertex.JOB.match(pipeline_run or "")
    require(location, "pipeline_run", "--pipeline-run is a Vertex AI pipeline job: projects/P/locations/L/pipelineJobs/J")
    found = _module("discover").propose(catalog, vertex.client(location.group(2)), pipeline_run, trainer=trainer, loader=loader,
                                         loader_output=loader_output, model=model, version=version)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(found["manifest"], f, indent=2)
            f.write("\n")
    return {**found, "written_to": out}


# ---- what a person reads on the command line --------------------------------------------------------------------
def _find_text(result, params):
    return [f"{r['key']}" + (f"  [{r['layer']}]" if r.get("layer") else "") + (f"  used by {r['used_by']}" if r.get("used_by") else "")
            + ("  (retired)" if r.get("retired") else "") + (f"\n    {r['description']}" if r["description"] else "") for r in result] or ["Nothing found."]


def _lineage_text(result, params):
    out = [result["key"]]
    for direction in ("upstream", "downstream"):
        if direction in result:
            out += [direction + ":"] + ([f"  {'  ' * (n['depth'] - 1)}{n['key']}" + _flag(n) for n in result[direction]] or ["  nothing"])
    return out


def _documents_text(result, params):
    asked = short(params["key"]).lower()
    return [f"{d['document']}" + (f"  #{d['section']}" if d["section"] else "") + (f"  {d['says']}" if d["says"] else "")
            + (f"  (about {d['through']})" if short(d["through"]) != asked else "") + ("  (redefined since the page described it)" if d["changed_since"] else "")
            for d in result] or ["No document describes this."]


def _used_by_text(result, params):
    return [f"{result['key']} is used by {len(result['projects'])} project(s)"] + [
        f"  {p['project']}  {p['how']}" + (f" through {p['through']}" if p["how"] != "direct" else "")
        + (f"  for: {p['purpose']}" if p["purpose"] else "") + _flag(p) for p in result["projects"]]


def _uses_text(result, params):
    out = [f"{result['key']} uses {len(result['uses'])} table(s) or column(s)"] + [
        f"  {u['key']}" + (f"  for: {u['purpose']}" if u.get("purpose") else "") + _flag(u) for u in result["uses"]]
    return out + (["built from:"] + [f"  {n['key']}" for n in result["built_from"]] if "built_from" in result else [])


def _diff_text(result, params):
    head = (f"{result['key']}: {result['severity'] or 'no change'}"
            + f"  ({result['from']['revision'] if result['from'] else 'not registered'} -> {result['to']['revision']})")
    return [head] + [f"  {c['severity']:<9} {short(c['key'])}: {c['change']}" + _was(c) for c in result["changes"]]


def _quality_text(result, params):
    return [f"{result['key']}: {result['status'] or 'no results'}" + (f" (as of {result['at']})" if result["at"] else "")] + [
        f"  {r['status']:<5} {short(r['entry'])}  {r['test']}" + (f"  {r['failures']} failing" if r["failures"] else "")
        + f"  ({r['observed_at']}, {r['source']})" + ("" if r.get("current", True) else "  (before its latest revision)") for r in result["results"]]


def _history_text(result, params):
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


def _explain_dataset_text(result, params):
    if result.get("kind") == "gcs_files":
        produced, exported = result["produced_by"], result["exported_from"]
        out = [result["reference"], f"  files         {result['uri']}  ({_count(len(result['objects']), 'object')}, generations pinned)",
               "  written by    " + (f"{produced['pipeline_run']} / task {produced['task']}  {produced['state']}" if produced
                                    else _created(result.get("created_by"))),
               "  exported from " + (f"{exported['table']}" + ("  (changed since)" if exported["changed_since"] else "") if exported else "not recorded")]
        for j in result.get("exported_by") or []:
            out.append(f"  exported by   BigQuery job ({j['job']})  {j['statement_type']}" + (f"  {j['rows']:,} rows" if j["rows"] is not None else ""))
        if result.get("row_filter"):
            out.append(f"  row filter    {result['row_filter']['expression'] or ', '.join(result['row_filter']['columns'])}")
        out += [f"  {c['field']} = {c.get('expression') or '?'}" + (f"  from {', '.join(d['column'] for d in c['derived_from'])}" if c["derived_from"] else "")
                + ("" if c["lineage"] == "resolved" else f"  ({c['lineage']})") if "derived_from" in c else f"  {c['field']}  {c['dtype'] or '?'}"
                for c in result["columns"]]
        return out + [f"missing: {m}" for m in result["missing"]]
    produced, job, defined = result["produced_by"], result["executed_as"], result["defined_in"]
    out = [result["reference"], f"  snapshot of   {result['snapshot_of']}",
           f"  produced by   {produced['dataform_invocation']} / {produced['target']}  {produced['state']}",
           "  executed as   " + (f"BigQuery job ({job['project']}, {job['location']}, {job['job_id']})  {job['statement_type']}" if job else "unknown"),
           f"  defined in    {_defined(defined)}"]
    for c in result["columns"]:
        out.append(f"  {c['field']} = {c['expression'] or '?'}" + (f"  from {', '.join(d['column'] for d in c['derived_from'])}" if c["derived_from"] else "")
                   + ("" if c["lineage"] == "resolved" else f"  ({c['lineage']})"))
    return out + [f"missing: {m}" for m in result["missing"]]


def _explain_model_text(result, params):
    produced, artifact, read = result["produced_by"], result["artifact"], result["read"]
    out = [f"{result['model']} / {result['version']}", f"  produced by   {produced['pipeline_run']} / task {produced['task']}",
           f"  artifact      {artifact['uri']}#{artifact['generation']}  sha256 {artifact['sha256'][:12]}…"]
    if read.get("kind") == "gcs_files":
        out += [f"  read          {read['uri']}  ({_count(read['objects'], 'object')}, generations pinned)",
                "  written by    " + (f"{read['produced_by']['pipeline_run']} / task {read['produced_by']['task']}" if read["produced_by"]
                                     else _created(read.get("created_by"))),
                "  exported from " + (f"{read['exported_from']['table']}" + ("  (changed since)" if read["exported_from"]["changed_since"] else "")
                                      if read["exported_from"] else "not recorded")]
        for j in read.get("exported_by") or []:
            out.append(f"  exported by   BigQuery job ({j['job']})  {j['statement_type']}" + (f"  {j['rows']:,} rows" if j["rows"] is not None else ""))
        if read.get("row_filter"):
            out.append(f"  row filter    {read['row_filter']['expression'] or ', '.join(read['row_filter']['columns'])}")
        out += [f"  {i['role']:<8} {i['field']} = {i['expression'] or '?'}  from {', '.join(i['derived_from']) or '?'}" if "derived_from" in i
                else f"  {i['role']:<8} {i['field']}  {i['dtype'] or '?'}  (lineage not claimed)" for i in result["inputs"]]
    else:
        out.append(f"  read          {read['dataset_version']}, snapshot of {read['snapshot_of']}")
        for i in result["inputs"]:
            out += [f"  {i['role']:<8} {i['field']} = {i['expression'] or '?'}" + ("" if i["lineage"] == "resolved" else f"  ({i['lineage']})"),
                    f"           built by {i['produced_by']['dataform_invocation']}" if i["produced_by"]["dataform_invocation"] else "           built by ?",
                    "           as " + (f"BigQuery job ({i['executed_as']['project']}, {i['executed_as']['location']}, {i['executed_as']['job_id']})"
                                        if i["executed_as"] else "an unknown job") + f", from {_defined(i['defined_in'])}",
                    f"           from {', '.join(i['derived_from']) or '?'}"]
    out += (["  relied on"] + _relied(result["relied_on"])) if result.get("relied_on") else []
    return out + [f"missing: {m}" for m in result["missing"]]


def _import_text(result, params):
    """What an import did. Each importer says it in its own words, told apart by where it read from."""
    if params.get("quality") or params.get("dbt_tests"):
        return [f"{result['added']} results added, {result['unchanged']} already kept"] + [f"  not registered: {u}" for u in result["unresolved"]]
    if params.get("docs"):
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
    if params.get("openlineage"):
        events = result["events"]
        recorded = sum(e["change"] == "recorded" for e in events)
        runs = {e["run"]: e for e in events if e["run"]}  # each run once, as it stands after its last event
        return [f"{len(events)} events: {recorded} recorded, {len(events) - recorded} duplicate"] + [f"  {e['job']}  {e['state']}" for e in runs.values()]
    if params.get("vertex_manifest"):
        checked = ("already recorded, so the artifact was not read again" if result["artifact_verified"] == "recorded"
                   else f"artifact checked by {result['artifact_verified']}")
        return [f"{result['model']} {result['version']}  {result['change']}  ({result['inputs']} input(s), {checked})"]
    # the importers of tables and columns: each says its own first line, and all say the rest alike
    counts = f"{result['added']} added, {result['changed']} changed, {result['unchanged']} unchanged"
    if params.get("dataform"):
        head = [f"{counts} from {result['compilation_result']}"] + (
            ["Definitions not applied: a newer compilation of this repository was imported before"] if result["definitions"] != "applied" else []) + [
            f"  {a['target']}  {a['state']}" + (f"  job {a['job_id']}" if a["job_id"] else "") for a in result["actions"]]
    elif params.get("postgres"):
        head = [f"{result['relations']} relations from {result['database']}: {result['added']} entries added, {result['changed']} changed, "
                f"{result['unchanged']} unchanged; {result['views_traced']} views traced, {result['foreign_keys']} foreign keys"]
    elif params.get("sql_definitions"):
        head = [f"{result['definitions']} definitions: {result['added']} entries added, {result['changed']} changed, {result['unchanged']} unchanged; "
                f"{result['columns_traced']} columns traced, {result['columns_partial']} in part"]
    else:
        head = [counts]
    return head + _import_rest(result)


def _import_rest(result: dict) -> list[str]:
    """What every import of tables and columns says after its first line: what it could not link, trace or register,
    and what is gone from its source."""
    return ([f"  not linked, its target is not registered: {u}" if " -> " in u else f"  not defined or registered, so registered by name only: {u}"
             for u in result.get("unresolved") or []]
            + [f"  SQL not traced (sqlglot could not read its columns): {t}" for t in result.get("untraced") or []]
            + [f"  skipped: {x}" for x in result.get("skipped") or []]
            + [f"  retired (no longer there): {k}" for k in result.get("retired") or []]
            + [f"  withdrawn (another source still says it): {k}" for k in result.get("withdrawn") or []])


def _sync_text(result, params):
    return [f"{result['jobs']} jobs read, {result['added']} new, from {result['since']} to {result['until']}"
            + (" (this window only: the next sync starts where the last one ended)" if result.get("window") == "explicit" else "")
            + (f" ({result['excluded']} monitoring jobs left out)" if result["excluded"] else "")
            + ("" if result["finished"] else f"\nNot recorded as finished: Google could not reach {', '.join(result['unreachable'])}")]


def _record_text(result, params):
    return [f"{result['reference']}  {result['change']}  ({_count(result['objects'], 'object')}, {_count(result['fields'], 'field')})"] + (
        [f"fields not known: {result['schema']}"] if result["schema"] != "read" else []) + (
        [f"exported by {', '.join(result['exported_by'])}"] if result.get("exported_by") else [])


def _privacy_export_text(result, params):
    import csv
    import io
    out = io.StringIO()
    fields = ["key", "table", "column", "dtype", "pii", "scrubbing", "data_category", "retention", "owner", "table_pii", "inherited"]
    writer = csv.DictWriter(out, fields, lineterminator="\n")
    writer.writeheader()
    for c in result:
        writer.writerow({**c, **{f: {True: "yes", False: "no", None: ""}[c[f]] for f in ("pii", "table_pii")}, "inherited": " ".join(c["inherited"])})
    return [out.getvalue().rstrip("\n")]


def _proposals_text(result, params):
    return ([f"{p['confidence']:<6} {short(p['key'])}  {p['data_category']}: {p['reason']}" for p in result["proposals"]] or ["Nothing to propose."]) + (
        [f"Written to {result['written_to']}: set accept to true on the right ones, then odcl privacy apply {result['written_to']}"]
        if result.get("written_to") else [])


def _alerts_send_text(result, params):
    out = [("Would send" if result["dry_run"] else "Sent") + f" {len(result['sent'])} message(s)" + (f"; {len(result['failed'])} failed" if result["failed"] else "")]
    for m in result["sent"]:
        out += [f"  subscription {m['subscription']} ({m['channel']}): {len(m['alerts'])} alert(s)"] + (
            ["    " + line for line in m["text"].splitlines()] if result["dry_run"] else [])
    return (out + [f"  subscription {m['subscription']} failed: {m['error']}" for m in result["failed"]]
            + [f"  subscription {m['subscription']} waits until {m['next_attempt']} after {m['attempts']} failed tries: {m['last_error']}"
               for m in result.get("deferred") or []])


def _delivery_text(delivery: dict | None) -> str:
    if not delivery or delivery["status"] == "ok":
        return ""
    when = f"next try {delivery['next_attempt']}" if delivery["next_attempt"] else "tried again next run"
    return f"\n    FAILING since {delivery['since']}, {delivery['attempts']} tries, {when}: {delivery['last_error']}"


def _subscriptions_text(result, params):
    return [f"{s['id']}  {s['scope']}{' ' + s['target'] if s['target'] else ''}  -> {s['channel']} {s['address']}  ({s['min_severity']} and above, "
            f"by {s['created_by']})" + _delivery_text(s.get("delivery")) for s in result] or ["No subscription."]


def _token_text(result, params):
    return [f"A token for {result['user']} ({result['id']}). It is shown this once; keep it secret:", "", f"  {result['token']}", "",
            "Use it as ODCL_TOKEN=... for the CLI and odcl mcp, or as Authorization: Bearer ... over HTTP."]


def _whoami_text(r, params) -> list[str]:
    if "user" in r:  # through a server: who the token signs in as
        return [_user(r["user"])] if r["user"] else ["Not signed in: set $ODCL_TOKEN to an API token to write, or to read from a server that asks for sign-in."]
    if r.get("direct"):
        return [f"{r['name']}: a direct database connection, without a token. Writes are recorded under this name."]
    return [_user(r)]


def _propose_manifest_text(result, params):
    out = [json.dumps(result["manifest"], indent=2), "", "Where each value came from:"] + [f"  {k}: {v}" for k, v in result["sources"].items()]
    out += ["", "Ready to import." if result["ready"] else "Review before importing:"] + [f"  - {r}" for r in result["review"]]
    out += [f"  missing: {', '.join(result['missing'])}"] if result["missing"] else []
    return out + ([f"\nWritten to {result['written_to']}: odcl import --vertex-manifest {result['written_to']}"] if result["written_to"] else [])


# ---- params several operations share ----------------------------------------------------------------------------
KEY_HELP = "A table or column, e.g. table:analytics.gold.accounts or column:analytics.gold.accounts.plan. A bare path works when only one entry has it."
KEY = Param("key", cli="positional", mcp="key", required=True, mcp_help=KEY_HELP)
PRIVACY = (Param("pii", "whether it holds personal data", choices=("yes", "no", True, False)),
           Param("data_category", "e.g. contact, financial, health; a column without one takes its table's", metavar="CATEGORY"),
           Param("retention", "how long it is kept, e.g. 400d or 7 years; a column without one takes its table's", metavar="PERIOD"),
           Param("scrubbing", "how the personal data is scrubbed, e.g. hashed, truncated, removed", metavar="HOW"))
ENTRY = (Param("description"), Param("owner"), Param("tags", kind="list", nargs="+", default=[]))
LINEAGE = (Param("derived_from", "what it is built from", kind="list", nargs="+", default=[], metavar="KEY"),)
ROLE_CHOICES = ("reader", "editor", "importer", "admin")


def _ops() -> list[Operation]:
    from . import users
    return [
        # what is there: the MCP tools come in this order
        Operation("find", "search tables, columns and projects", lambda c, query="", kind=None, layer=None, limit=50, retired=False:
                  c.find(query, kind, layer, limit, retired=bool(retired)),
                  (Param("query", kind="words", cli="positional", nargs="*", metavar="WORDS", http="q", mcp="query", default="",
                         mcp_help="Words every result must contain. Empty lists everything."),
                   Param("kind", choices=("table", "column", "project", "document", "term", "domain"), mcp="kind"),
                   Param("layer", mcp="layer", mcp_help="Only data of this layer, e.g. bronze, silver or gold."),
                   Param("limit", kind="int", default=50, mcp="limit", mcp_schema={"minimum": 1, "maximum": 200}),
                   Param("retired", "include tables and columns no longer at their source", kind="bool")),
                  cli="find", http="GET /v1/search", mcp="find_data", mcp_wrap="results", text=_find_text,
                  mcp_help="Search tables, columns and projects by the words in their name, description, definition, owner or tags. "
                           "Each result says its layer, its owner and how many projects use it. Start here to find the right data."),
        Operation("show", "one entry in full", lambda c, key: c.get(key), (KEY,), cli="show", http="GET /v1/entries/{key}", mcp="get_entry",
                  mcp_help="One table, column or project in full: its description and definition, what it is built from, what is built from "
                           "it, which projects use it, the code that defines it, and its history."),
        Operation("lineage", "what a table or column is built from, and what is built from it", lambda c, key, direction="both": c.lineage(key, direction),
                  (KEY, Param("direction", choices=("up", "down", "both"), default="both", mcp="direction")),
                  cli="lineage", http="GET /v1/entries/{key}/lineage", mcp="trace_lineage", text=_lineage_text,
                  mcp_help="Upstream: what a table or column is built from, back to source data. Downstream: everything a change to it reaches."),
        Operation("documents", "the documents that describe a table, column or project", lambda c, key: c.documents(key), (KEY,),
                  cli="docs", http="GET /v1/entries/{key}/documents", mcp="documents_for", mcp_wrap="documents", text=_documents_text,
                  mcp_help="The documents (knowledge-base pages, findings, runbooks) that describe a table, column or project, with the section "
                           "of each page and what it says. A column also gets the pages that describe its table. Says when the entry was redefined "
                           "after a page described it."),
        Operation("used_by", "which projects depend on a table or column", lambda c, key: c.used_by(key), (KEY,),
                  cli="used-by", http="GET /v1/entries/{key}/used-by", mcp="used_by", text=_used_by_text,
                  mcp_help="Which projects depend on a table or column: directly, through its table or columns, or through data built from it. "
                           "Use this before changing or dropping data."),
        Operation("diff", "what changed in a table (with its columns) or a column, each change labelled by how much it matters",
                  lambda c, key, since=None, until=None: c.diff(key, since, until),
                  (KEY, Param("since", "a revision (rev_...), a time, or a span back from now such as 7d (default: its previous revision)", mcp="since",
                              mcp_help="A revision (rev_…), a time (ISO 8601) or a span back from now such as 7d."),
                   Param("until", "as --since (default: now)", mcp="until", mcp_help="As since; default now.")),
                  cli="diff", http="GET /v1/entries/{key}/diff", mcp="what_changed", text=_diff_text,
                  mcp_help="What changed in a table or column between two points: each change (a type, a definition, a column added, removed "
                           "or retired, a description) labelled cosmetic, additive, semantic or breaking. With no points, its latest change. A "
                           "project's use pins a revision: pass it as `since` to see what changed after the project used it."),
        Operation("uses", "what a project used", lambda c, name, upstream=False, revision=None: c.uses(name, bool(upstream), revision),
                  (Param("name", cli="positional", metavar="PROJECT", required=True, mcp="project", mcp_help="A project name or key, e.g. project:churn"),
                   Param("upstream", "also everything that data is built from", kind="bool", mcp="upstream",
                         mcp_help="Also list everything that data is built from."),
                   Param("revision", "what this older revision of the project had pinned", metavar="REV")),
                  cli="uses", http="GET /v1/projects/{name}/uses", mcp="project_uses", text=_uses_text,
                  mcp_help="What data a project used, and whether any of it changed after the project used it."),
        Operation("graph", "lineage around a table or column as every node and edge", lambda c, key, direction="both", depth=None, projects=False, level="column":
                  c.graph(key, direction, depth, bool(projects), level),
                  (KEY, Param("direction", choices=("up", "down", "both"), default="both"), Param("depth", kind="int"),
                   Param("projects", "also the projects that use what is drawn", kind="bool"), Param("level", choices=("column", "table"), default="column")),
                  cli="graph", http="GET /v1/entries/{key}/graph"),
        Operation("quality", "the newest result of each data-quality test on a table (and its columns) or a column",
                  lambda c, key, at=None: _module("quality").for_entry(c, key, at),
                  (KEY, Param("at", "as of a time, or a span back from now such as 7d (default: now)")),
                  cli="quality", http="GET /v1/entries/{key}/quality", text=_quality_text),
        Operation("overview", "how much is registered, and what changed after a project used it", lambda c: c.overview(), cli="overview", http="GET /v1/overview"),
        Operation("impact", "what each project is exposed to through the data it uses: changed, gone or failing a test",
                  lambda c, project=None: _module("impact").impact(c, project), (Param("project", "only this project"),),
                  cli="impact", http="GET /v1/impact",
                  text=lambda r, p: [f"{short(i['project'])} uses {short(i['key'])}: {i['kind']}" + (f" ({i['severity']})" if i["severity"] and i["kind"] == "changed" else "")
                                     + "".join(f"\n    {short(c['key'])}: {c['change']} [{c['severity']}]" for c in i["changes"][:5])
                                     + "".join(f"\n    {short(t['entry'])}: {t['test']} {t['status']}" for t in i.get("failing", [])) for i in r]
                  or ["Every project uses data as it is defined now, still at its source and passing its tests."]),
        Operation("tables", "every table, with its columns and users counted", lambda c, layer=None, system=None, retired=False, sort="key":
                  c.tables(layer, system, retired=bool(retired), sort=sort),
                  (Param("layer"), Param("system"), Param("retired", kind="bool"), Param("sort", choices=("key", "reads"), default="key")),
                  cli="tables", http="GET /v1/tables",
                  text=lambda r, p: [f"{t['key']}" + (f"  [{t['layer']}]" if t.get("layer") else "") + f"  {_count(t['columns'], 'column')}"
                                     + (f", used by {t['used_by']}" if t.get("used_by") else "") + (f", {t['reads']} reads" if t.get("reads") else "")
                                     + ("  (retired)" if t.get("retired") else "") for t in r] or ["No table is registered."]),
        Operation("projects", "every project, and what changed after it used it", lambda c: c.projects(), cli="projects", http="GET /v1/projects",
                  text=lambda r, p: [f"{x['key']}  uses {x['uses']}" + (f", {x['changed_since_used']} changed since" if x["changed_since_used"] else "")
                                     + (f", {_count(x['model_versions'], 'model version')} (latest {x['latest_version']})" if x["model_versions"] else "")
                                     for x in r] or ["No project is registered."]),
        Operation("models", "model versions", lambda c, project=None: c.models(project), (Param("project"),), cli="models", http="GET /v1/models",
                  text=lambda r, p: [f"{m['model']} {m['version']}  trained on {m['dataset_version']}  ({m['recorded_at']})" for m in r]
                  or ["No model version is recorded."]),
        Operation("runs", "recorded runs", lambda c, system=None, limit=100: c.runs(system, limit), (Param("system"), Param("limit", kind="int", default=100)),
                  cli="runs", http="GET /v1/runs"),
        Operation("history", "the query jobs that read or wrote a table", _history,
                  (Param("table", "a table key or its BigQuery path", cli="positional", metavar="KEY", required=True),
                   Param("since", "how far back to look, like 7d or 12h (default: 7d)", default="7d", metavar="SPAN"),
                   Param("patterns", "also group the jobs by query shape", kind="bool"), Param("limit", kind="int", default=100)),
                  cli="history table", http="GET /v1/query-history", text=_history_text),
        Operation("popular", "the tables most read by query jobs, from the synced job history", _popular,
                  (Param("since", "a span back from now (default: 30d)", default="30d"), Param("limit", kind="int", default=50)),
                  cli="popular", http="GET /v1/popular",
                  text=lambda r, p: [f"{t['table']}  {t['reads']} reads, {t['shapes']} query shapes, last {t['last_read']}" + ("" if t["key"] else "  (not registered)")
                                     for t in r["tables"]] or [f"No job read a table in the last {r['since']}. Run odcl sync bigquery-jobs first."]),
        Operation("glossary", "every glossary term, with its domain and how many tables and columns mean it", lambda c, domain=None: c.glossary(domain),
                  (Param("domain"),), cli="glossary", http="GET /v1/glossary",
                  text=lambda r, p: [f"{t['title']}" + (f"  [{short(t['domain'])}]" if t["domain"] else "") + f"  meant by {t['meant_by']}"
                                     + (f"  ({t['status']})" if t["status"] else "") + f"\n    {t['definition']}"
                                     + (f"\n    also: {', '.join(t['synonyms'])}" if t["synonyms"] else "") for t in r] or ["No glossary term yet."]),
        Operation("explain_dataset", "a dataset version: its build, job, definition and each field's inputs",
                  lambda c, reference: _module("builds").explain_dataset(c, reference),
                  (Param("reference", "project.dataset.snapshot, or project.dataset.snapshot@time", cli="positional", required=True),),
                  cli="explain dataset", http="GET /v1/datasets/{reference}/trace", text=_explain_dataset_text),
        Operation("explain_model", "a model version: its training task, artifact, data and each input's lineage", lambda c, name, version: c.explain_model(name, version),
                  (Param("name", cli="positional", required=True), Param("version", required=True)),
                  cli="explain model", http="GET /v1/models/{name}/versions/{version}/trace", text=_explain_model_text),
        Operation("privacy", "every column with its privacy fields, as CSV (or --json)", lambda c, pii_only=False, table=None: c.privacy(bool(pii_only), table),
                  (Param("pii_only", "only columns marked as personal data", kind="bool"), Param("table", "only this table's columns")),
                  cli="privacy export", http="GET /v1/privacy", text=_privacy_export_text),
        Operation("privacy_proposals", "columns that look like personal data, with a data category and why, for review; writes nothing", _proposals,
                  (Param("table", "only this table's columns"), Param("all", "also columns that already say whether they are personal data", kind="bool"),
                   Param("min_confidence", choices=("low", "medium", "high"), default="low"),
                   Param("write", "write the proposals as JSON Lines to review: set accept to true on the ones that are right", metavar="FILE", http=None)),
                  cli="privacy propose", http="GET /v1/privacy/proposals", text=_proposals_text),

        # annotations people make
        Operation("add_project", "register a project by hand", _put("project"),
                  (Param("name", cli="positional", required=True, mcp="name"), Param("description", required=True, mcp="description"),
                   Param("owner", mcp="owner"), Param("repository", mcp="repository"), Param("status", mcp="status"), ENTRY[2]),
                  role="editor", cli="add project", http="POST /v1/projects", mcp="register_project", text=_revision,
                  mcp_help="Register a project: anything that uses data, such as an analysis or a model."),
        Operation("use", "record that a project used tables or columns", lambda c, name, keys, purpose=None: c.use(name, keys, purpose),
                  (Param("name", cli="positional", metavar="PROJECT", required=True, mcp="project", mcp_help="A project name or key, e.g. project:churn"),
                   Param("keys", kind="list", cli="positional", nargs="+", metavar="KEY", required=True, mcp="keys",
                         mcp_help="Tables or columns the project used."),
                   Param("purpose", "what the project used them for", cli="--for", mcp="purpose", mcp_help="What the project used them for.")),
                  role="editor", cli="use", http="POST /v1/projects/{name}/uses", mcp="record_use", text=_revision,
                  mcp_help="Record that a project used these tables or columns, at their current revision."),
        Operation("add_table", "register a table by hand", _put("table"),
                  (Param("path", "database.schema.name", cli="positional", required=True), Param("system"), Param("layer"), Param("type"),
                   *ENTRY, *LINEAGE, *PRIVACY), role="editor", cli="add table", text=_revision),
        Operation("add_column", "register a column by hand", _put("column"),
                  (Param("table", cli="positional", required=True), Param("name", cli="positional", required=True), Param("dtype"),
                   Param("definition", "how it is computed"),
                   Param("references", "the column(s) its values are drawn from: a foreign key", kind="list", nargs="+", default=[], metavar="COLUMN"),
                   *ENTRY, *LINEAGE, *PRIVACY), role="editor", cli="add column", text=_revision),
        Operation("privacy_set", "say these for tables or columns, keeping what else they say",
                  lambda c, keys, pii=None, data_category=None, retention=None, scrubbing=None:
                  {"changed": c.set_privacy(keys, pii=pii, data_category=data_category, retention=retention, scrubbing=scrubbing)},
                  (Param("keys", kind="list", cli="positional", nargs="+", metavar="KEY", required=True), *PRIVACY),
                  role="editor", cli="privacy set", http="POST /v1/privacy",
                  text=lambda r, p: [f"{e['key']}  {e['change']}  ({e['revision']})" for e in r["changed"]]),
        Operation("privacy_apply", "record the accepted proposals in a reviewed file (all or none)", lambda c, file: _module("pii").apply_review(c, file),
                  (Param("file", cli="positional", required=True),), role="editor", cli="privacy apply",
                  text=lambda r, p: [f"{len(r['accepted'])} recorded, {r['skipped']} left as they were"] + [f"  {short(k)}" for k in r["accepted"]]),
        Operation("created_by", "say what created a table or file: a script, a notebook, a query run by hand", _created_by,
                  (Param("key", "the table, view or file entry", cli="positional", metavar="TABLE", required=True),
                   Param("what", "a local file (its repository, commit and link are read from git), a link, or a path in --repo-url",
                         cli="positional", nargs="?", http=None),
                   Param("kind", "default: from the file's extension", choices=("script", "notebook", "query", "job", "manual", "other"), http=None),
                   Param("command_line", "how it was run, e.g. python make_features.py --month 2026-10", cli="--command", metavar="CMD", http=None),
                   Param("note", "anything else worth knowing", http=None), Param("repo_url", "the repository, when the file is not in a local checkout", http=None),
                   Param("commit", "the commit, when git cannot say", http=None), Param("clear", "forget what created it", kind="bool", http=None),
                   Param("created_by", kind="any", cli=None)),
                  role="editor", cli="created-by", http="POST /v1/entries/{key}/created-by", remote=_created_by_remote,
                  text=lambda r, p: _revision(r) + [f"  created by {_created(r.get('created_by'))}" if r.get("created_by") else "  nothing recorded"]),
        Operation("references", "say which columns a column's values are drawn from: a foreign key", lambda c, key, columns=None: c.set_references(key, columns or []),
                  (Param("key", cli="positional", metavar="COLUMN", required=True),
                   Param("columns", "the referenced column(s); none clears them", kind="list", cli="positional", nargs="*", metavar="COLUMN")),
                  role="editor", cli="references", http="POST /v1/entries/{key}/references",
                  text=lambda r, p: _revision(r) + [f"  references {link['to']}" for link in r["links"] if link["relation"] == "references"]),
        Operation("instances", "say which tables are copies of a definition table, by patterns over their paths",
                  lambda c, key, patterns=None: c.set_instances(key, patterns or []),
                  (Param("key", "the definition table, e.g. core.sales.daily_totals", cli="positional", metavar="DEFINITION", required=True),
                   Param("patterns", "globs over table paths, e.g. 't-*.sales.daily_totals'; none clears them", kind="list", cli="positional", nargs="*",
                         metavar="PATTERN")),
                  role="editor", cli="instances", http="POST /v1/entries/{key}/instances",
                  text=lambda r, p: _revision(r) + ([f"  copy: {link['to']}" for link in r["links"] if link["relation"] == "instance_of"] or ["  no table matches yet"])),
        Operation("rely", "record that a model version relied on documents, such as a finding, at the revision it was trained on",
                  lambda c, name, version, documents, why=None: c.rely(name, version, documents, why),
                  (Param("name", cli="positional", metavar="MODEL", required=True), Param("version", required=True),
                   Param("documents", kind="list", cli="positional", nargs="+", metavar="DOCUMENT", required=True), Param("why", "what the model took from them")),
                  role="editor", cli="rely", http="POST /v1/models/{name}/versions/{version}/relied-on",
                  text=lambda r, p: [f"{r['model']} {r['version']} relied on:"] + _relied(r["relied_on"])),
        Operation("add_term", "add or change a glossary term: a business word and what it means", _put("term", merge=True),
                  (Param("name", cli="positional", required=True), Param("description", cli="--definition", required=True),
                   Param("synonyms", "another word for it; repeat for more", kind="list", cli="--synonym", append=True),
                   Param("domain"), Param("owner"), Param("status", "draft, approved, or your own word")),
                  role="editor", cli="term add", http="POST /v1/terms", text=_revision),
        Operation("add_domain", "add or change a domain: a part of the business that owns data, projects and terms", _put("domain", merge=True),
                  (Param("name", cli="positional", required=True), Param("description", required=True), Param("owner")),
                  role="editor", cli="domain add", http="POST /v1/domains", text=_revision),
        Operation("means", "say which glossary terms a table or column means (no terms clears them)", lambda c, key, terms=None: c.set_terms(key, terms or []),
                  (Param("key", cli="positional", required=True), Param("terms", kind="list", cli="positional", nargs="*")),
                  role="editor", cli="means", http="POST /v1/entries/{key}/terms", text=_revision),
        Operation("in_domain", "put a table, project or term in a domain (no domain takes it out)", lambda c, key, domain=None: c.set_domain(key, domain),
                  (Param("key", cli="positional", required=True), Param("domain", cli="positional", nargs="?")),
                  role="editor", cli="in-domain", http="POST /v1/entries/{key}/domain", text=_revision),

        # alerts
        Operation("alerts_subscribe", "send alerts about a project, an owner's projects or every project (editor)", _subscribe,
                  (Param("project", group="scope!"), Param("owner", "every project whose owner is this", group="scope!"),
                   Param("all", "every project", kind="bool", group="scope!"),
                   Param("webhook", "POST each message as JSON to this https:// address", metavar="URL", group="to!", http=None),
                   Param("slack", "a Slack incoming webhook", metavar="URL", group="to!", http=None),
                   Param("email", "through the SMTP server in $ODCL_SMTP_HOST", metavar="ADDRESS", group="to!", http=None),
                   Param("channel", cli=None), Param("address", cli=None),
                   Param("min_severity", choices=("cosmetic", "additive", "semantic", "breaking"), default="semantic")),
                  role="editor", cli="alerts subscribe", http="POST /v1/alerts/subscriptions", remote=_subscribe_remote,
                  text=lambda r, p: [f"subscription {r['id']}: {r['scope']}{' ' + r['target'] if r['target'] else ''} -> {r['channel']} {r['address']}"]),
        Operation("alerts_list", "subscriptions: your own, or every one for an admin", lambda c: _module("alerts").subscriptions(c),
                  cli="alerts list", http="GET /v1/alerts/subscriptions", text=_subscriptions_text),
        Operation("alerts_remove", "remove a subscription (editor)", lambda c, id: _module("alerts").unsubscribe(c, id),  # noqa: A006
                  (Param("id", kind="int", cli="positional", required=True),), role="editor", cli="alerts remove",
                  http="POST /v1/alerts/subscriptions/{id}/remove", text=lambda r, p: [f"removed subscription {r['removed']['id']}"]),
        Operation("alerts_send", "send what is new since the last run; run it after imports (importer)",
                  lambda c, dry_run=False: _module("alerts").send(c, dry_run=bool(dry_run)), (Param("dry_run", "say what would be sent, and send nothing", kind="bool"),),
                  role="importer", cli="alerts send", http="POST /v1/alerts/send", text=_alerts_send_text),

        # users and tokens
        Operation("me", "the signed-in user, or null", lambda c: {"user": c.user}, role="anyone", http="GET /v1/me"),
        Operation("whoami", "who this connection writes as", users.whoami, cli="users whoami", remote=lambda p: ("me", {}),
                  text=_whoami_text),
        Operation("users_list", "every user (admin)", users.listing, role="admin", cli="users list", http="GET /v1/users",
                  text=lambda r, p: [_user(u) for u in r] or ["No users yet. Make the first admin: odcl users add NAME --role admin"]),
        Operation("users_add", "make a user: a person, or a service user for importers (admin)", users.add,
                  (Param("name", cli="positional", required=True), Param("role", choices=ROLE_CHOICES, required=True), Param("display_name"),
                   Param("email", "a person who later signs in through single sign-on with this verified email becomes this user")),
                  role="admin", cli="users add", http="POST /v1/users", text=lambda r, p: [_user(r)]),
        Operation("users_set", "change a user's role, or disable or enable them (admin)", _set_user,
                  (Param("name", cli="positional", required=True), Param("role", choices=ROLE_CHOICES), Param("disabled", kind="bool", cli=None),
                   Param("disable", "their tokens and sessions stop working", kind="bool", group="on", http=None),
                   Param("enable", kind="bool", group="on", http=None)),
                  role="admin", cli="users set", http="POST /v1/users/{name}", text=lambda r, p: [_user(r)],
                  remote=lambda p: ("users_set", {"name": p["name"], "role": p.get("role"),
                                                  "disabled": True if p.get("disable") else False if p.get("enable") else None})),
        Operation("token", "make an API token, shown once: your own, or (admin) a user's", users.make_token,
                  (Param("name", cli="positional", nargs="?", http=None), Param("label", "what it is for, e.g. ci or laptop")),
                  cli="users token", http="POST /v1/me/tokens", text=_token_text,
                  remote=lambda p: ("token_for", p) if p.get("name") else ("token", p)),  # another user's token: an admin's route
        Operation("token_for", "make an API token for a user (admin)", users.make_token, (Param("name", cli=None), Param("label")),
                  role="admin", http="POST /v1/users/{name}/tokens", text=_token_text),
        Operation("tokens", "the current tokens of a user (yours, or admin: anyone's)", users.tokens, (Param("name", cli="positional", nargs="?", http=None),),
                  cli="users tokens", http="GET /v1/me/tokens",
                  text=lambda r, p: [f"{t['id']}  {t['label'] or ''}  made {t['created_at']}" + (f"  last used {t['last_used_at']}" if t["last_used_at"] else "") for t in r]),
        Operation("revoke", "stop a token working", lambda c, id: _module("users").revoke(c, id),  # noqa: A006
                  (Param("id", "its id, tok_...", cli="positional", required=True),),
                  cli="users revoke", http="POST /v1/me/tokens/{id}/revoke", text=lambda r, p: [f"{r['id']} ({r['user']}) no longer works."]),

        # reading from elsewhere: the command line only, as they call Google or read local files
        Operation("import", "read tables and columns from a dbt project, BigQuery, PostgreSQL, Dataform and more", _import,
                  (Param("dbt", "a dbt target folder or its manifest.json", metavar="PATH"),
                   Param("select", "with --dbt: only these models and what they are built from", kind="list", nargs="+", metavar="MODEL"),
                   Param("bigquery", "BigQuery tables to read, as project.dataset.table", kind="list", nargs="+", metavar="TABLE"),
                   Param("dataform", "a Dataform workflow invocation, as projects/P/locations/L/repositories/R/workflowInvocations/I", metavar="INVOCATION"),
                   Param("vertex_manifest", "a training manifest, as a local file or gs:// object, written by a Vertex AI training task", metavar="PATH"),
                   Param("docs", "Markdown pages, or folders of them, whose frontmatter has an odcl block", kind="list", nargs="+", metavar="PATH"),
                   Param("repo_url", "with --docs: the repository's web address, when git cannot say", metavar="URL"),
                   Param("commit", "with --docs: the commit the pages were read at, when git cannot say", metavar="SHA"),
                   Param("openlineage", "OpenLineage events: one JSON event, a JSON list of them, or JSON Lines", metavar="FILE"),
                   Param("postgres", "a PostgreSQL database to read tables, views, view lineage and foreign keys from, e.g. postgresql://reader@host/dbname "
                                     "(the URL is not kept)", metavar="URL"),
                   Param("schema", "with --postgres: only these schemas (default: all but the system's)", kind="list", nargs="+", metavar="SCHEMA"),
                   Param("database_name", "with --postgres: the name to key its tables by (default: the database's own)", metavar="NAME"),
                   Param("sql_definitions", "JSON Lines of tables and the SELECT that builds each: target, select, dependencies, description, columns, "
                                            "type, file (or declaration: true)", metavar="FILE"),
                   Param("system", "with --sql-definitions: the system the tables live in (default: the dialect)"),
                   Param("quality", "data-quality results as JSON Lines: entry, test, status, failures, observed_at, run, kind, message", metavar="FILE"),
                   Param("dbt_tests", "dbt test results: a target folder with run_results.json and manifest.json", metavar="PATH"),
                   Param("source", "with --quality: where the results come from, such as great_expectations (default: quality)"),
                   Param("dialect", "with --sql-definitions: the SQL dialect, as sqlglot names it (default: bigquery)", default="bigquery"),
                   Param("project", "with --bigquery: the project the API calls are made from (default: your credentials' project)")),
                  role="importer", cli="import", text=_import_text),
        Operation("record_quality", "keep data-quality results (importer)", lambda c, source, results: _module("quality").record(c, results, source),
                  (Param("source", required=True), Param("results", kind="any", required=True)), role="importer", http="POST /v1/quality"),
        Operation("sync", "the query jobs a BigQuery project ran since the last sync", _sync,
                  (Param("project", "the project the jobs ran in, which may differ from where the tables live", required=True),
                   Param("backfill", "how far back the first sync reads (default: 7d)", default="7d", metavar="SPAN"),
                   Param("since", "read exactly the jobs created from this time (ISO 8601, or a span back from now like 12h); the regular sync still "
                                  "starts where it last ended", metavar="TIME"),
                   Param("until", "read the jobs created up to this time (default: now)", metavar="TIME"),
                   Param("pipeline_run", "read the jobs created while this Vertex AI pipeline job ran", metavar="JOB"),
                   Param("task", "with --pipeline-run: only while this task of it ran"),
                   Param("sql", "templates keeps each query's shape without its literals; none does not read SQL at all", choices=("templates", "none"),
                         default="templates")),
                  role="importer", cli="sync bigquery-jobs", text=_sync_text),
        Operation("handoff", "record that a successful build handed on a snapshot of its output", _handoff,
                  (Param("invocation", "the Dataform workflow invocation that built the table", required=True),
                   Param("target", "the table the build wrote, as project.dataset.table", required=True),
                   Param("snapshot", "the table snapshot taken of it, as project.dataset.table", required=True),
                   Param("project", "the project BigQuery calls are made from (default: your credentials' project)")),
                  role="importer", cli="handoff", text=lambda r, p: [f"{r['reference']}  {r['change']}"]),
        Operation("record_files", "the Cloud Storage files a training run read, such as a data-loader task's Parquet output", _record_files,
                  (Param("pipeline_run", "the Vertex AI pipeline job whose task wrote the files", metavar="JOB"),
                   Param("task", "with --pipeline-run: the task that wrote them"),
                   Param("output", "with --pipeline-run: the task's output artifact, e.g. output_train_dataset", metavar="NAME"),
                   Param("uri", "instead: gs:// files or folders not written by a pipeline task", kind="list", nargs="+", metavar="GS_URI"),
                   Param("from_table", "the catalog table the files were exported from (table-level lineage only)", metavar="TABLE"),
                   Param("export_job", "the BigQuery EXPORT DATA job that wrote the files; each field is traced through its SELECT (repeat for several)",
                         kind="list", append=True, metavar="PROJECT:LOCATION.JOB_ID"),
                   Param("find_export_job", "with --pipeline-run: find the EXPORT DATA job among the jobs created while the task ran", kind="bool"),
                   Param("sql", "keep the export job's SQL as its shape (default), or not at all", choices=("templates", "none"), default="templates"),
                   Param("created_by", "what wrote the files, when no pipeline task did: a script or notebook (a local file, read from git) or a link",
                         metavar="SCRIPT"),
                   Param("created_by_note", "how it was run, or anything else worth knowing", metavar="TEXT")),
                  role="importer", cli="record dataset-files", text=_record_text),
        Operation("discover_vertex_runs", "the Vertex AI pipeline runs of a project, and which already have a model version recorded", _discover,
                  (Param("project", required=True), Param("location", default="us-central1"),
                   Param("since", "runs created since this time or span (default: 7d)", default="7d", metavar="TIME")),
                  cli="discover vertex-runs",
                  text=lambda r, p: [f"{x['created'] or '?'}  {x['state'] or '?':<26}  {x['pipeline_run']}" + (f"  {x['display_name']}" if x["display_name"] else "")
                                     + (f"\n    recorded: {', '.join(x['model_versions'])}" if x["model_versions"] else "") for x in r["runs"]]
                  or ["No pipeline run in that window."]),
        Operation("propose_vertex_manifest", "a training manifest for a pipeline run, from its trainer's metadata.json and its loader's output", _propose_manifest,
                  (Param("pipeline_run", required=True, metavar="JOB"),
                   Param("trainer", "the task that trained the model (default: the one with a Model output)", metavar="TASK"),
                   Param("loader", "the task that wrote the training data (default: the one with a Dataset output)", metavar="TASK"),
                   Param("loader_output", "the loader's output the model read (default: the one named for training)", metavar="NAME"),
                   Param("model", "the model's name, when metadata.json does not say it"),
                   Param("version", "the model version, when metadata.json does not say it (default: the pipeline job's id)"),
                   Param("out", "also write the draft manifest here, to review and then import with --vertex-manifest", metavar="FILE")),
                  cli="propose vertex-manifest", text=_propose_manifest_text),
    ]


OPERATIONS: list[Operation] = _ops()
BY_NAME = {op.name: op for op in OPERATIONS}
GROUPS = {  # the command line's groups of commands, and what each is for
    "add": "register a table, a column or a project by hand",
    "alerts": "tell people when data their projects use changes after they used it, or is gone",
    "discover": "find runs whose model versions the catalog has not recorded",
    "domain": "add or change a domain: a part of the business that owns data, projects and terms",
    "explain": "where something came from, as recorded",
    "history": "the query jobs that read or wrote something",
    "privacy": "which tables and columns hold personal data, their data category and retention",
    "propose": "draft something for a person to review before it is imported",
    "record": "record a dataset version that is not a BigQuery snapshot",
    "sync": "read new activity from a source into the catalog",
    "term": "add or change a glossary term: a business word and what it means",
    "users": "who may read and write the catalog, and their API tokens",
}


def _check() -> None:
    """The table holds together: names, routes, tools and commands are each used once, and every {name} in a route
    is a param of its operation."""
    import re
    for attr in ("name", "cli", "http", "mcp"):
        seen = [getattr(op, attr) for op in OPERATIONS if getattr(op, attr)]
        assert len(seen) == len(set(seen)), f"two operations share a {attr}"
    for op in OPERATIONS:
        assert op.role in ROLES, op.name
        names = {p.name for p in op.params}
        for path_name in re.findall(r"\{(\w+)\}", op.http or ""):
            assert path_name in names, f"{op.name}: {path_name} in its route is not a param"


_check()


def operation(name: str) -> Operation:
    found = BY_NAME.get(name)
    if found is None:
        raise CatalogError("operation", f"No operation {name}")
    return found
