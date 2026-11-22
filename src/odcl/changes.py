"""What changed in an entry between two points in its history, and how much it matters to what uses it.

Every change is labelled with a severity, from least to most:

- `cosmetic`: what a person reads changed (description, owner, tags, layer), not the data.
- `additive`: something new that nothing could rely on yet (a column added, an entry revived).
- `semantic`: the data may now mean something else (a column's definition or inputs, a privacy field).
- `breaking`: what used it may now fail (a column's type changed, or a column or table removed or retired).

A table's diff includes its columns, since a table's schema is its columns, and each column is its own entry with
its own revisions. Points in history are revisions or times; revisions are ordered by `seq`, which only increases.
"""
from __future__ import annotations

from datetime import datetime

from .store import CatalogError, require, stamp

SEVERITY = ("cosmetic", "additive", "semantic", "breaking")
IGNORED = {"key", "kind", "title", "path", "table", "name", "links", "source", "retired", "retired_by", "headings", "digest", "commit", "url"}
FIELDS = {  # field -> (severity, what it means)
    "dtype": ("breaking", "type changed"),
    "definition": ("semantic", "definition changed"),
    "text": ("semantic", "text changed"),
    "pii": ("semantic", "personal data flag changed"),
    "scrubbing": ("semantic", "scrubbing changed"),
    "data_category": ("semantic", "data category changed"),
    "retention": ("semantic", "retention changed"),
    "instances": ("semantic", "copies changed"),
}
LINKS = {"derived_from": ("semantic", "computed from different inputs"), "references": ("semantic", "foreign key changed"),
         "instance_of": ("semantic", "copies changed"), "describes": ("cosmetic", "what it describes changed"),
         "uses": ("cosmetic", "what it uses changed"), "means": ("cosmetic", "glossary terms changed"),
         "in_domain": ("cosmetic", "domain changed")}


def worst(severities) -> str | None:
    found = [s for s in severities if s]
    return max(found, key=SEVERITY.index) if found else None


def _links(body: dict | None, relation: str) -> set[str]:
    return {link["to"] for link in (body or {}).get("links") or [] if link["relation"] == relation}


def body_changes(before: dict | None, after: dict | None) -> list[dict]:
    """The changes from one revision's body to another's, each with its severity. None is "not registered"."""
    alive = lambda body: body is not None and not body.get("retired")
    if not alive(before) and not alive(after):
        return []
    if not alive(before):
        return [{"field": None, "change": "revived" if before else "added", "severity": "additive", "before": None, "after": None}]
    if not alive(after):
        what = "retired" if after else "removed"
        return [{"field": None, "change": what, "severity": "breaking", "before": None, "after": None}]
    out = []
    for field in sorted((set(before) | set(after)) - IGNORED):
        if before.get(field) in (None, "", []) and after.get(field) in (None, "", []):
            continue
        if before.get(field) != after.get(field):
            severity, what = FIELDS.get(field, ("cosmetic", f"{field.replace('_', ' ')} changed"))
            if field == "dtype" and (before.get(field) in (None, "") or after.get(field) in (None, "")):
                severity, what = "cosmetic", "type stated" if after.get(field) else "type no longer stated"  # not a type change
            out.append({"field": field, "change": what, "severity": severity, "before": before.get(field), "after": after.get(field)})
    for relation, (severity, what) in LINKS.items():
        was, now = _links(before, relation), _links(after, relation)
        if was != now:
            out.append({"field": relation, "change": what, "severity": severity, "before": sorted(was), "after": sorted(now),
                        "added": sorted(now - was), "removed": sorted(was - now)})
    return out


def _point(history, value, default: int) -> int:
    """A point in history as a `seq`: a revision (of this entry, or any entry), a time (ISO 8601, or a span back
    from now such as 7d), or `default`."""
    if value is None or value == "":
        return default
    if isinstance(value, str) and not value.startswith("rev_"):
        from .times import moment  # a time, or a span back from now such as 7d
        value = moment(value, "since")
    if isinstance(value, str):
        found = history.point(value)
        require(found is not None, "revision", f"No revision {value}")
        return found
    require(isinstance(value, datetime) and value.tzinfo is not None, "since", "Name a point as a revision (rev_…) or a time with a time zone")
    return history.point_at(value)


def diff(catalog, name: str, since=None, until=None) -> dict:
    """What changed in an entry (and, for a table, its columns) from `since` to `until`.

    Each is a revision id or a time. `until` defaults to now. `since` defaults to the revision before the entry's
    revision at `until`, so with neither, the diff is the entry's latest change."""
    history = catalog.history
    key = catalog.resolve(name)
    revisions = history.revisions(key)
    until_seq = _point(history, until, history.now())
    upto = [r for r in revisions if r["seq"] <= until_seq]
    require(upto, "until", f"{key} was not registered yet at that point")
    since_seq = _point(history, since, upto[-2]["seq"] if len(upto) > 1 else upto[-1]["seq"] - 1)
    require(since_seq <= until_seq, "since", "since is after until")

    def at(rows, seq):
        found = [r for r in rows if r["seq"] <= seq]
        return found[-1] if found else None

    def side(row) -> dict | None:
        return {"revision": row["id"], "at": stamp(row["recorded_at"]), "by": row["actor"]} if row else None

    first, last = at(revisions, since_seq), at(revisions, until_seq)
    changes = [{"key": key, **c} for c in body_changes(first and first["body"], last and last["body"])]
    if last["body"]["kind"] == "table":
        for column, past in sorted(history.column_revisions(key, until_seq).items()):
            was, now_ = at(past, since_seq), at(past, until_seq)
            changes += [{"key": column, **c} for c in body_changes(was and was["body"], now_ and now_["body"])]
    return {"key": key, "kind": last["body"]["kind"], "from": side(first), "to": side(last), "severity": worst(c["severity"] for c in changes),
            "changes": changes}


def severity_since_use(catalog, project: str, key: str, revision: str) -> str | None:
    """The worst change to an entry since a project used it. The use is dated by the first revision of the project
    that pinned this revision of the entry: a table's columns may have been registered after the table's revision,
    and before the use."""
    try:
        return diff(catalog, key, since=catalog.history.link_started(project, "uses", key, revision) or revision)["severity"]
    except CatalogError:
        return None
