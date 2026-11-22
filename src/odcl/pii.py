"""Proposed privacy fields: which columns look like personal data, for a person to confirm.

Nothing here writes. `propose` reads columns that say nothing about `pii` yet and suggests `pii` and a
`data_category` from the words in each column's name, the glossary terms it means, and its description, with
the reason and a confidence. `write_review` puts the proposals in a JSON Lines file a person edits (set `accept`
on the ones that are right, change what is wrong), and `apply_review` records the accepted ones through
`Catalog.set_privacy`, all or none. Reading no rows, it cannot tell a column of names from one of product
names; that is what the review is for.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .history import retired_sql
from .store import Catalog, CatalogError, require

LEVELS = ("low", "medium", "high")
PEOPLE = {"customer", "customers", "user", "users", "person", "people", "employee", "employees", "patient", "patients", "member", "members",
          "contact", "contacts", "account", "accounts", "client", "clients", "staff", "student", "students", "applicant", "applicants"}
# (words that must all be among the column's words, category, confidence, what it suggests). The first rule to match wins.
RULES = [
    ({"ssn"}, "government_id", "high", "a social security number"),
    ({"social", "security"}, "government_id", "high", "a social security number"),
    ({"passport"}, "government_id", "high", "a passport number"),
    ({"national", "id"}, "government_id", "high", "a national identifier"),
    ({"tax", "id"}, "government_id", "high", "a tax identifier"),
    ({"driver", "license"}, "government_id", "high", "a driving licence"),
    ({"email"}, "contact", "high", "an email address"),
    ({"phone"}, "contact", "high", "a phone number"),
    ({"mobile"}, "contact", "high", "a phone number"),
    ({"first", "name"}, "identity", "high", "a person's name"),
    ({"last", "name"}, "identity", "high", "a person's name"),
    ({"full", "name"}, "identity", "high", "a person's name"),
    ({"surname"}, "identity", "high", "a person's name"),
    ({"dob"}, "identity", "high", "a date of birth"),
    ({"birth"}, "identity", "high", "a date of birth"),
    ({"card", "number"}, "financial", "high", "a payment card number"),
    ({"iban"}, "financial", "high", "a bank account"),
    ({"account", "number"}, "financial", "medium", "an account number"),
    ({"diagnosis"}, "health", "high", "health information"),
    ({"medical"}, "health", "high", "health information"),
    ({"ethnicity"}, "special_category", "high", "a special category of personal data"),
    ({"religion"}, "special_category", "high", "a special category of personal data"),
    ({"gender"}, "demographic", "medium", "a demographic attribute"),
    ({"salary"}, "financial", "medium", "pay"),
    ({"address"}, "contact", "medium", "an address"),
    ({"street"}, "contact", "medium", "an address"),
    ({"postcode"}, "contact", "medium", "a postal code"),
    ({"zip"}, "contact", "medium", "a postal code"),
    ({"ip"}, "online_identifier", "medium", "an IP address"),
    ({"latitude"}, "location", "medium", "a location"),
    ({"longitude"}, "location", "medium", "a location"),
    ({"lat"}, "location", "medium", "a location"),
    ({"lon"}, "location", "medium", "a location"),
    ({"lng"}, "location", "medium", "a location"),
    ({"username"}, "online_identifier", "medium", "a user name"),
    ({"name"}, "identity", "low", "a name, if it is a person's"),
]
IDS = ("customer", "user", "employee", "patient", "member", "person", "client", "student", "applicant")


def words(text: str) -> set[str]:
    """The words in a name or a sentence: split at anything not a letter, and at a change from lower to upper case."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")
    return {w for w in re.split(r"[^a-z]+", text.lower()) if w}


def _judge(name: str, said: set[str], table_words: set[str]) -> tuple[str, str, str] | None:
    """(category, confidence, reason) for one column, or None. `said` is every word for it: name, terms, description."""
    name_words = words(name)
    for needed, category, confidence, what in RULES:
        if needed <= name_words:
            if needed == {"name"} and table_words & PEOPLE:  # "name" on a table of people is a person's name
                confidence = "medium"
            return category, confidence, f"its name looks like {what}"
    for needed, category, confidence, what in RULES:
        if needed != {"name"} and needed <= said:
            return category, "low" if confidence != "high" else "medium", f"its glossary terms or description mention {what}"
    for who in IDS:  # customer_id, user_id: a key that points at a person, so it identifies them where the rest of their record is
        if {who, "id"} <= name_words:
            return "identifier", "medium", f"it identifies a {who}"
    return None


def propose(catalog: Catalog, table: str | None = None, everything: bool = False, minimum: str = "low") -> list[dict]:
    """Proposed `pii` and `data_category` for columns that say nothing about `pii` yet (every column with `everything`),
    at `minimum` confidence or above. Writes nothing."""
    require(minimum in LEVELS, "confidence", f"A confidence is one of: {', '.join(LEVELS)}")
    table = catalog.resolve(table, ("table",)) if table else None
    out = []
    for r in catalog.db.execute(f"""
            SELECT c.key, cr.body AS col, tr.body AS tbl,
                   ARRAY(SELECT tb.body ->> 'title' || ' ' || coalesce(tb.body ->> 'description', '') || ' ' ||
                                coalesce(array_to_string(ARRAY(SELECT jsonb_array_elements_text(tb.body -> 'synonyms')), ' '), '')
                         FROM revision_links l JOIN entries te ON te.key = l.target_key JOIN revisions tb ON tb.id = te.head_revision
                         WHERE l.source_revision = cr.id AND l.relation = 'means') AS terms
            FROM entries c JOIN revisions cr ON cr.id = c.head_revision JOIN entries t ON t.key = cr.body ->> 'table'
            JOIN revisions tr ON tr.id = t.head_revision
            WHERE c.kind = 'column' AND cr.body ->> 'kind' = 'column' AND NOT {retired_sql('cr')}
              AND (%(table)s::text IS NULL OR cr.body ->> 'table' = %(table)s) ORDER BY c.key""", {"table": table}):
        col, tbl = r["col"], r["tbl"]
        if col.get("pii") is not None and not everything:
            continue
        said = words(col["name"]) | words(" ".join(r["terms"])) | words(col.get("description") or "")
        judged = _judge(col["name"].split(".")[-1], said, words(tbl.get("path", "").split(".")[-1]) | words(tbl.get("description") or ""))
        if not judged or LEVELS.index(judged[1]) < LEVELS.index(minimum):
            continue
        category, confidence, reason = judged
        out.append({"key": r["key"], "pii": True, "data_category": category, "confidence": confidence, "reason": reason,
                    "now": {"pii": col.get("pii"), "data_category": col.get("data_category")}})
    return out


def write_review(proposals: list[dict], path: str) -> str:
    """A JSON Lines file of proposals for a person to edit: set "accept" to true on the ones that are right."""
    file = Path(path).expanduser()
    file.write_text("".join(json.dumps({**{k: p[k] for k in ("key", "pii", "data_category", "confidence", "reason")}, "accept": False}) + "\n"
                            for p in proposals), encoding="utf-8")
    return str(file)


def apply_review(catalog: Catalog, path: str) -> dict:
    """Record the accepted lines of a reviewed proposal file: their `pii` and `data_category`. All or none."""
    file = Path(path).expanduser()
    require(file.is_file(), "privacy", f"{path} is not a file")
    accepted = []
    for n, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as e:
            raise CatalogError("privacy", f"{file.name} line {n} is not JSON: {e.msg}") from None
        require(isinstance(item, dict) and isinstance(item.get("key"), str), "privacy", f"{file.name} line {n}: name the column as key")
        require(isinstance(item.get("accept", False), bool), "privacy", f"{file.name} line {n}: accept is true or false")
        if item.get("accept"):
            require(isinstance(item.get("pii"), bool), "privacy", f"{file.name} line {n}: pii is true or false")
            accepted.append(item)
    with catalog.db.transaction():
        for item in accepted:
            catalog.set_privacy([item["key"]], pii=item["pii"], data_category=item.get("data_category") or None)
    return {"accepted": [i["key"] for i in accepted], "skipped": sum(1 for line in file.read_text(encoding="utf-8").splitlines() if line.strip()) - len(accepted)}
