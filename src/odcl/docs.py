"""Documents: Markdown pages that describe catalog entries, such as a team's knowledge-base pages.

A page says what it describes in its frontmatter, so its body stays as it is:

    ---
    title: "Order status codes"
    owner: Data Team
    odcl:
      kind: reference                 # reference | finding | runbook | glossary: free text, short
      describes:
        - key: column:warehouse.sales.orders.status_code
          section: status-codes       # optional: a heading anchor in this page
          says: "Closed = status_code IN ('C', 'X')"   # optional, one line
        - pattern: "column:*-prod.sales.orders.amount*"   # a glob over entry keys
    ---

A key names one entry. A pattern is resolved at every import to one link per entry it matches, and is kept on the
document, so a later import links entries added since. A key or pattern that matches nothing is not an error: it is
reported in the import result and on the document, as is a section the page has no heading for.

The document's commit and web address come from git when the page is in a checkout, else from --commit and
--repo-url. A page imported before and gone now (or that no longer has an odcl block) is retired: a new revision that
describes nothing, so its links stay as history but are no longer current. Every file of one import is written in
one transaction.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from fnmatch import fnmatchcase
from pathlib import Path

from .dbt import code_ref, web_url
from .store import DESCRIBED, Catalog, CatalogError, require

FENCE = re.compile(r"^\s{0,3}(```|~~~)")
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")


def frontmatter(content: str) -> tuple[dict | None, str]:
    """The YAML frontmatter of a page, and the body after it. No frontmatter is (None, content)."""
    try:
        import yaml
    except ImportError as e:  # pragma: no cover - depends on the installation
        raise CatalogError("docs", "Importing documents needs PyYAML: install open-data-context-layer[docs]") from e
    if not content.startswith("---"):
        return None, content
    end = re.search(r"^---\s*$", content[3:], re.M)
    if not end or content[3:4] not in ("\n", "\r"):
        return None, content
    try:
        meta = yaml.safe_load(content[3:3 + end.start()])
    except yaml.YAMLError as e:
        raise CatalogError("docs", f"The frontmatter cannot be read: {str(e).splitlines()[0]}") from None
    return (meta if isinstance(meta, dict) else None), content[3 + end.end():].lstrip("\r\n")


def anchor(heading: str) -> str:
    """A heading's anchor as GitHub and most Markdown sites make it: lower-cased, punctuation dropped, spaces as -."""
    text = unicodedata.normalize("NFKC", heading).lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.strip().replace(" ", "-")


def headings(body: str) -> list[dict]:
    """Every ATX heading outside code blocks, with its anchor; a repeated anchor gets -1, -2, … as GitHub does."""
    out, seen, fenced = [], {}, None
    for line in body.splitlines():
        fence = FENCE.match(line)
        if fence:
            fenced = None if fenced == fence.group(1) else (fenced or fence.group(1))
            continue
        found = None if fenced else HEADING.match(line)
        if found:
            base = anchor(found.group(2))
            n = seen.get(base, 0)
            seen[base] = n + 1
            out.append({"text": found.group(2), "anchor": base if n == 0 else f"{base}-{n}", "level": len(found.group(1))})
    return out


def first_paragraph(body: str) -> str:
    for block in re.split(r"\n\s*\n", body):
        block = " ".join(line.strip() for line in block.strip().splitlines())
        if block and not block.startswith(("#", "|", "```", "~~~", "<", "-", "*", ">")):
            return block[:300]
    return ""


def _repository(url: str | None, fallback: str) -> str:
    """owner/repo from a repository's web address, else the folder's name."""
    found = re.match(r"https?://[^/]+/(.+?)/?$", url or "")
    return found.group(1).lower() if found else fallback.lower()


def _files(paths: list[str]) -> list[tuple[Path, Path]]:
    """(file, the folder or file it was named by) for every Markdown file under the paths."""
    out = []
    for given in paths:
        path = Path(given).expanduser()
        require(path.exists(), "docs", f"{given} does not exist")
        found = [path] if path.is_file() else sorted(p for p in path.rglob("*.md") if not any(part.startswith(".") for part in p.relative_to(path).parts))
        out += [(f, path) for f in found]
    return out


def read_page(catalog: Catalog, file: Path, named_by: Path, repo_url: str | None, commit: str | None) -> dict | None:
    """The document entry a page makes, or None when it has no odcl block."""
    content = file.read_text(encoding="utf-8")
    meta, body = frontmatter(content)
    block = (meta or {}).get("odcl")
    if not isinstance(block, dict):
        return None
    ref = code_ref(file) or {}
    url_base = repo_url or ref.get("repository")
    commit = commit or ref.get("commit")
    base = named_by if named_by.is_dir() else named_by.parent
    relative = ref.get("path") or file.resolve().relative_to(base.resolve()).as_posix()
    root = file.resolve()
    for _ in (ref["path"].split("/") if ref else []):
        root = root.parent  # the checkout's root, whose name stands in for the repository when it has no web address
    repository = _repository(url_base, root.name if ref else base.resolve().name)
    marks = headings(body)
    anchors = {h["anchor"] for h in marks}
    keys = [r["key"] for r in catalog.db.execute("SELECT key FROM entries WHERE kind = ANY(%s) ORDER BY key", (list(DESCRIBED),))]
    known = set(keys)
    describes, patterns, unresolved = [], [], []
    for item in block.get("describes") or []:
        require(isinstance(item, dict) and bool(item.get("key")) != bool(item.get("pattern")), "docs",
                f"{file}: each describes item has a key or a pattern, not both")
        section, says = item.get("section"), item.get("says")
        if item.get("key"):
            key = str(item["key"]).strip().lower()
            targets = [key] if key in known else []
            if not targets:
                unresolved.append(key)
        else:
            pattern = str(item["pattern"]).strip().lower()
            patterns.append(pattern)
            targets = [k for k in keys if fnmatchcase(k, pattern)]
            if not targets:
                unresolved.append(pattern)
        if section and str(section) not in anchors and targets:
            unresolved.append(f"section {section} of {item.get('key') or item.get('pattern')}")
        describes += [{"key": t, "section": str(section) if section else None, "says": str(says).strip() if says else None} for t in targets]
    return {"repository": repository, "path": relative, "title": str((meta or {}).get("title") or (marks[0]["text"] if marks else file.stem)),
            "owner": str(meta["owner"]) if meta.get("owner") else None, "doc_kind": str(block["kind"]) if block.get("kind") else None,
            "description": first_paragraph(body), "text": body, "headings": marks,
            "url": f"{url_base}/blob/{commit}/{relative}" if url_base and commit else url_base or None, "commit": commit,
            "digest": hashlib.sha256(content.encode()).hexdigest(), "describes": describes, "patterns": patterns, "unresolved": unresolved}


def import_docs(catalog: Catalog, paths: list[str], *, repo_url: str | None = None, commit: str | None = None) -> dict:
    """Import every Markdown page with an odcl block under `paths` (files or folders), and retire the pages of the
    same repository and folders that were imported before and are gone. All of it, or none."""
    require(paths, "docs", "Name at least one Markdown file or folder")
    repo_url = web_url(repo_url) or repo_url if repo_url else None
    counts, skipped, unresolved, seen, scopes = {"added": 0, "changed": 0, "unchanged": 0, "retired": 0}, 0, {}, set(), set()
    with catalog.db.transaction():
        for file, named_by in _files(paths):
            page = read_page(catalog, file, named_by, repo_url, commit)
            if page is None:
                skipped += 1
                continue
            written = catalog.put("document", page)
            counts[written["change"]] += 1
            seen.add(written["key"])
            if page["unresolved"]:
                unresolved[written["key"]] = page["unresolved"]
            if named_by.is_dir():  # a folder imported: the pages under it that are gone are retired
                inside = file.resolve().relative_to(named_by.resolve()).as_posix()
                scopes.add((page["repository"], page["path"].removesuffix(inside)))
        for repository, prefix in scopes:
            for r in catalog.db.execute("""SELECT r.body FROM entries e JOIN revisions r ON r.id = e.head_revision
                                           WHERE e.kind = 'document' AND r.body ->> 'repository' = %s AND (r.body ->> 'path') LIKE %s
                                             AND NOT COALESCE((r.body ->> 'retired')::boolean, false)""",
                                        (repository, prefix.replace("%", r"\%").replace("_", r"\_") + "%")).fetchall():
                body = r["body"]
                if body["key"] not in seen:
                    catalog.put("document", {**{k: body.get(k) for k in ("repository", "path", "title", "owner", "doc_kind", "description", "text",
                                                                           "headings", "url", "commit", "digest", "patterns", "unresolved")},
                                             "describes": [], "retired": True})
                    counts["retired"] += 1
    return {"documents": counts, "skipped": skipped, "unresolved": unresolved}
