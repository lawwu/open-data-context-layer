"""Read a dbt project into catalog entries: every source and model as a table, every column, and the lineage between them.

Table lineage comes from dbt's own dependency graph. Column lineage is read from each model's compiled SQL by
`lineage.column_parents`, so nothing here parses SQL by hand. Each entry records the file that defines it and the commit that last
changed that file, so re-importing after unrelated commits changes nothing.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from . import lineage
from .lineage import column_parents
from .store import require

LAYERS = ("bronze", "silver", "gold")
UNSAFE = re.compile(r"[^a-z0-9_\-$]+")


def clean(name) -> str:
    """A name the catalog accepts, for a table or column the warehouse allowed to be anything."""
    return UNSAFE.sub("_", str(name).lower()).strip("_")[:150] or "unnamed"


def layer_of(meta, tags, schema) -> str | None:
    """The layer a project names in `meta.layer`, else a bronze/silver/gold tag, else one in the schema name."""
    named = (meta or {}).get("layer") if isinstance(meta, dict) else None
    if isinstance(named, str) and named.strip():
        return named.strip()[:50]
    for tag in tags or []:
        if str(tag).lower() in LAYERS:
            return str(tag).lower()
    return next((layer for layer in LAYERS if layer in str(schema or "").lower().split("_")), None)


def privacy_of(meta) -> dict:
    """`pii`, `data_category`, `retention` and `scrubbing` from a dbt `meta`, directly or under `meta.privacy`."""
    if not isinstance(meta, dict):
        return {}
    found = {**meta, **(meta.get("privacy") if isinstance(meta.get("privacy"), dict) else {})}
    out = {k: str(found[k])[:200] for k in ("data_category", "retention", "scrubbing") if found.get(k) not in (None, "")}
    if isinstance(found.get("pii"), bool) or str(found.get("pii")).lower() in ("yes", "no", "true", "false"):
        out["pii"] = found["pii"] if isinstance(found["pii"], bool) else str(found["pii"]).lower() in ("yes", "true")
    return out


def web_url(remote: str) -> str | None:
    """https://host/owner/repo for a Git remote. Anything before an @ (a user name or a token) is dropped."""
    match = re.fullmatch(r"(?:https?://|ssh://)?(?:[^@/]+@)?([A-Za-z0-9.\-]+)[:/]+(.+?)(?:\.git)?/?", remote or "")
    return f"https://{match.group(1)}/{match.group(2)}" if match and "." in match.group(1) else None


def code_ref(path) -> dict | None:
    """Where a definition lives: the repository, the file, and the commit that last changed that file."""
    path = Path(path).resolve()
    if not path.is_file():
        return None
    git = lambda *argv: subprocess.run(["git", "-C", str(path.parent), *argv], capture_output=True, text=True)
    root, commit = git("rev-parse", "--show-toplevel"), git("log", "-1", "--format=%H", "--", path.name)
    if root.returncode or commit.returncode or not commit.stdout.strip():
        return None
    repository = web_url(git("config", "--get", "remote.origin.url").stdout.strip())
    relative = path.relative_to(Path(root.stdout.strip()).resolve()).as_posix()
    return {"repository": repository, "commit": commit.stdout.strip(), "path": relative,
            "url": f"{repository}/blob/{commit.stdout.strip()}/{relative}" if repository else None}


def dbt_entries(path, select=None) -> list[tuple[str, dict]]:
    """(kind, payload) entries, dependencies first, for a dbt target folder or its manifest.json.

    By default every source and model is read. `select` names models; then only those and what they are built
    from are read. Column types come from catalog.json when `dbt docs generate` has written one."""
    path = Path(path)
    manifest_path = path / "manifest.json" if path.is_dir() else path
    require(manifest_path.is_file(), "dbt", f"No dbt manifest at {manifest_path}. Run `dbt compile` (or `dbt run`) first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    catalog_path = manifest_path.parent / "catalog.json"
    types = json.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path.is_file() else {"nodes": {}, "sources": {}}
    root = manifest_path.parent.parent
    metadata = manifest.get("metadata") or {}
    dialect = metadata.get("adapter_type") or None
    nodes = {uid: n for uid, n in manifest.get("nodes", {}).items() if n.get("resource_type") in ("model", "seed", "snapshot")}
    nodes.update(manifest.get("sources", {}))
    models = {uid: n for uid, n in nodes.items() if n.get("resource_type") == "model"}
    require(models, "dbt", "The dbt project holds no models")
    parents_of = lambda uid: [p for p in (nodes[uid].get("depends_on") or {}).get("nodes", []) if p in nodes]
    if select:
        by_name = {n["name"]: uid for uid, n in models.items()}
        missing = [name for name in select if name not in by_name]
        require(not missing, "dbt", f"No dbt model named {', '.join(missing)} (models: {', '.join(sorted(by_name))})")
        wanted, stack = set(), [by_name[name] for name in select]
        while stack:
            uid = stack.pop()
            if uid not in wanted:
                wanted.add(uid)
                stack += parents_of(uid)
    else:
        wanted = set(nodes)

    relation = lambda n: ".".join(clean(p) for p in (n.get("database"), n.get("schema"), n.get("identifier") or n.get("alias") or n["name"]) if p)
    raw_relation = lambda n: ".".join(p for p in (n.get("database"), n.get("schema"), n.get("identifier") or n.get("alias") or n["name"]) if p).lower()
    by_relation = {raw_relation(n): uid for uid, n in nodes.items()}
    selected: dict[str, list[str]] = {}

    def outputs(uid) -> list[str]:
        """The columns a model's SQL selects, for a project that has not described them and has no catalog of types."""
        if uid not in selected:
            selected[uid] = []
            if nodes[uid].get("compiled_code") and nodes[uid].get("resource_type") == "model":
                selected[uid] = lineage.selected(nodes[uid]["compiled_code"], dialect)
        return selected[uid]

    def typed(uid) -> dict:
        found = (types.get("nodes", {}).get(uid) or types.get("sources", {}).get(uid) or {}).get("columns", {})
        return {name.lower(): (c.get("type") or None) for name, c in found.items()}

    def columns(uid) -> dict:
        """lower-cased column name -> what the project says about it, for every column the warehouse or the YAML knows."""
        declared = {name.lower(): c for name, c in (nodes[uid].get("columns") or {}).items()}
        return {name: {**declared.get(name, {}), "data_type": typed(uid).get(name) or declared.get(name, {}).get("data_type")}
                for name in dict.fromkeys([*typed(uid), *declared, *outputs(uid)])}

    refs: dict = {}

    def defined_in(node) -> dict:
        file = node.get("original_file_path")
        if file not in refs:
            refs[file] = code_ref(root / file) if file else None
        out = {"tool": "dbt", "id": node["unique_id"], "project": metadata.get("project_name"), "file": file}
        return {**out, "code": refs[file]} if refs[file] else out

    def schema_for(node) -> dict:
        """The upstream relations' columns, in the nested shape sqlglot wants, so `select *` and unqualified names resolve."""
        out: dict = {}
        for parent in parents_of(node["unique_id"]):
            if columns(parent):
                p = nodes[parent]
                parts = [x.lower() for x in (p.get("database"), p.get("schema"), p.get("identifier") or p.get("alias") or p["name"]) if x]
                level = out
                for part in parts[:-1]:
                    level = level.setdefault(part, {})
                level[parts[-1]] = {name: c["data_type"] or "unknown" for name, c in columns(parent).items()}
        return out

    out: list[tuple[str, dict]] = []
    tables: set[str] = set()

    def table(uid) -> str:
        """Register a source or model as a table, after the tables it reads. Returns its key."""
        node = nodes[uid]
        key = "table:" + relation(node)
        if uid in tables:
            return key
        tables.add(uid)
        upstream = sorted({table(p) for p in parents_of(uid)} - {key})
        meta = node.get("meta") or node.get("source_meta") or (node.get("config") or {}).get("meta") or {}
        is_model = node.get("resource_type") == "model"
        out.append(("table", {
            "path": relation(node), "description": (node.get("description") or "").strip(), "system": dialect,
            "layer": layer_of(meta, node.get("tags"), node.get("schema")), "owner": meta.get("owner") if isinstance(meta.get("owner"), str) else None,
            "type": ((node.get("config") or {}).get("materialized") or "model") if is_model else node.get("resource_type"),
            "tags": sorted({str(t)[:60] for t in node.get("tags") or []}), "derived_from": upstream, "source": defined_in(node), **privacy_of(meta)}))
        return key

    done: dict[tuple[str, str], str | None] = {}

    def column(uid, name) -> str | None:
        """Register one column after every column it is computed from. Returns its key."""
        node = nodes.get(uid)
        # a source column the SQL reads is registered even when the project never described it
        if node is None or (name not in columns(uid) and node.get("resource_type") == "model"):
            return None
        if (uid, name) in done:
            return done[uid, name]
        key = done[uid, name] = f"column:{relation(node)}.{clean(name)}"  # set before its parents, so bad SQL cannot recurse forever
        info, is_model = columns(uid).get(name, {}), node.get("resource_type") == "model"
        payload = {"table": table(uid), "name": clean(name), "dtype": info.get("data_type"), "description": (info.get("description") or "").strip(),
                   "tags": [], "source": defined_in(node), **privacy_of(info.get("meta") or (info.get("config") or {}).get("meta"))}
        if is_model and node.get("compiled_code"):
            traced = False
            try:
                expression, found, traced = column_parents(name, node["compiled_code"], dialect, schema_for(node))
                payload["definition"] = (expression or "")[:4000] or None
                payload["derived_from"] = sorted({k for k in (column(by_relation.get(rel), col.lower()) for rel, col in found) if k and k != key})
            except Exception:  # noqa: BLE001 - SQL sqlglot cannot read still gets an entry, marked as not traced
                pass
            if not traced:
                payload["tags"] = ["lineage:partial"]
        out.append(("column", payload))
        return key

    for uid in sorted(wanted):
        table(uid)
        for name in columns(uid):
            column(uid, name)
    return out


def import_project(catalog, path, select=None) -> dict:
    """Register a dbt project's models, sources and columns, and retire what the project defined before and no
    longer does. With `select`, only part of the project is read, so nothing is retired."""
    entries = dbt_entries(path, select)
    projects = {p["source"].get("project") for _, p in entries if isinstance(p.get("source"), dict) and p["source"].get("tool") == "dbt"}
    withdraw = None if select else {"importer": "dbt", "scope": lambda said, _: said.get("project") in projects}
    return {"retired": [], "withdrawn": [], **catalog.apply(entries, withdraw=withdraw)}
