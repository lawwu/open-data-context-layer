"""What an entry can say, each field once: the kinds of entry it belongs to, how it is checked and stored, and
whether an import keeps it.

`Catalog.put` reads this table. For each field of the entry's kind it decides what the entry says (the payload's
value, or what the entry said before), then asks the field to `store` it: what goes in the revision's body, and the
links it makes. A new field is one entry here.

- A field with a `relation` is stored as links of that relation, pinned to the revision each target has now; what
  it said before is read back from those links. Any other field is read back from the body under its name.
- `beside` marks what people say beside what importers say: copies, what created a table, foreign keys, glossary
  terms, a domain, the privacy fields. A payload that leaves such a field out keeps what the entry says, even
  without `merge`; saying it as None clears it. So an importer that says the whole entry does not erase them.
- With `merge`, every field the payload leaves out, or says as None, keeps what the entry says.

Three things an entry says are not here, because `put` treats each in its own way: its description (required of a
project and a term), its tags (each importer owns its own, under `merge`) and its source (combined by importer).

The order of the fields with links is the order of an entry's links, and so part of what a revision says: keep it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .store import GLOB, PRIVACY, TEXT, names, require, text

DATA = ("table", "column")


@dataclass(frozen=True)
class Field:
    name: str
    kinds: tuple[str, ...]
    store: Callable[[Any, str, str, Any], tuple[dict, list[dict]]]  # (catalog, kind, key, value) -> (body, links)
    beside: bool = False
    relation: str | None = None
    single: bool = False

    def said(self, body: dict):
        """What a revision's body says for this field: its link targets, or the value under its name."""
        if self.relation is None:
            return body.get(self.name)
        targets = [link["to"] for link in body["links"] if link["relation"] == self.relation]
        return (targets[0] if targets else None) if self.single else targets


def _links(relation: str, targets: tuple[str, ...] | None, limit: int = 200, own: bool = False):
    """Store a list of keys as links of `relation` to entries of the `targets` kinds (None: the entry's own kind).
    With `own`, an entry may not point at itself."""
    def store(catalog, kind, key, value):
        made = []
        for target in names(value, relation if relation != "means" else "terms", limit):
            target = catalog.resolve(target, targets or (kind,))
            require(not own or target != key, relation, f"{key} cannot {'reference' if relation == 'references' else 'be derived from'} itself")
            made.append({"relation": relation, "to": target, "revision": catalog.history.head(target)})
        return {}, made
    return store


def _domain(catalog, kind, key, value):
    if not value:
        return {}, []
    domain = catalog.resolve(str(value), ("domain",))
    return {}, [{"relation": "in_domain", "to": domain, "revision": catalog.history.head(domain)}]


def _text(name: str, limit: int):
    return lambda catalog, kind, key, value: ({name: text(value, name, limit, optional=True)}, [])


def _kept_if_said(name: str, limit: int):
    def store(catalog, kind, key, value):
        said = text(value, name, limit, optional=True)
        return ({name: said} if said is not None else {}), []
    return store


def _pii(catalog, kind, key, value):
    if isinstance(value, str):
        require(value.lower() in ("yes", "no", "true", "false"), "pii", "pii is yes or no")
        value = value.lower() in ("yes", "true")
    require(value is None or isinstance(value, bool), "pii", "pii is yes or no")
    return ({"pii": value} if value is not None else {}), []


def _synonyms(catalog, kind, key, value):
    if not value:
        return {}, []
    synonyms = names(value, "synonyms", 20)
    require(all(len(x) <= 100 for x in synonyms), "synonyms", "A synonym is at most 100 characters")
    return {"synonyms": sorted(set(synonyms))}, []


def _instances(catalog, kind, key, value):
    if value is None:
        return {}, []
    patterns = sorted({i.lower().removeprefix("table:") for i in names(value, "instances", 50)})
    require(all(GLOB.match(i) for i in patterns), "instances", "An instance pattern is a table path with * ? or [ ] in it, like t-*.sales.orders")
    return ({"instances": patterns} if patterns else {}), catalog._instance_links(key, patterns)


def _created_by(catalog, kind, key, value):
    from .provenance import check
    created = check(value)
    return ({"created_by": created} if created else {}), []


def _text_fields() -> list[Field]:
    kinds: dict[str, tuple[int, list[str]]] = {}
    for kind, fields in TEXT.items():
        for name, limit in fields.items():
            kinds.setdefault(name, (limit, []))[1].append(kind)
    return [Field(name, tuple(of), _text(name, limit)) for name, (limit, of) in kinds.items()]


FIELDS: tuple[Field, ...] = (
    # stored as links, in the order an entry's links are kept
    Field("derived_from", DATA, _links("derived_from", None, own=True), relation="derived_from"),
    Field("references", ("column",), _links("references", ("column",), 20, own=True), beside=True, relation="references"),
    Field("terms", DATA, _links("means", ("term",), 20), beside=True, relation="means"),
    Field("domain", ("table", "project", "term"), _domain, beside=True, relation="in_domain", single=True),
    Field("synonyms", ("term",), _synonyms),
    Field("instances", ("table",), _instances, beside=True),
    # stored in the body
    *_text_fields(),
    Field("pii", DATA, _pii, beside=True),
    *(Field(name, DATA, _kept_if_said(name, limit), beside=True) for name, limit in PRIVACY.items()),
    Field("created_by", ("table",), _created_by, beside=True),
)


def of(kind: str) -> list[Field]:
    return [f for f in FIELDS if kind in f.kinds]
