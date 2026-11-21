"""What created a table or a file: a script, a notebook, a query someone ran by hand.

This is what a person says, not evidence the catalog checked: odcl cannot tell that the script ran, only where it
is. Given a local file, the repository, its path there, the commit that last changed it and a link come from git,
and a file with uncommitted changes says so, because then that commit is not the code that ran.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .store import require

KINDS = ("script", "notebook", "query", "job", "manual", "other")
SCRIPTS = (".py", ".r", ".sql", ".sh", ".scala", ".jl", ".js", ".ts", ".rb", ".go", ".java")
FIELDS = {"kind": 20, "path": 500, "repository": 300, "commit": 100, "url": 1000, "command": 1000, "note": 2000}


def check(value) -> dict | None:
    """A `created_by` object as an entry keeps it, or None when there is none."""
    if value in (None, {}):
        return None
    require(isinstance(value, dict), "created_by", "created_by is an object: kind, path, repository, commit, url, command, note")
    unknown = set(value) - set(FIELDS) - {"uncommitted"}
    require(not unknown, "created_by", f"created_by has no field {', '.join(sorted(unknown))}; use {', '.join(FIELDS)}")
    require(value.get("kind") in KINDS, "created_by", f"created_by.kind is one of: {', '.join(KINDS)}")
    out = {}
    for field, limit in FIELDS.items():
        said = value.get(field)
        require(said is None or (isinstance(said, str) and len(said) <= limit), "created_by", f"created_by.{field} is text of at most {limit} characters")
        if said:
            out[field] = said.strip()
    require(out.keys() - {"kind"}, "created_by", "Say where it came from: a path, a link, a command or a note")
    if value.get("uncommitted"):
        out["uncommitted"] = True
    return out


def describe(what: str | None, *, kind: str | None = None, command: str | None = None, note: str | None = None,
             repo_url: str | None = None, commit: str | None = None) -> dict:
    """`created_by` for a local file (read from git), a link, or a path in some repository named by `repo_url`."""
    from .dbt import code_ref, web_url
    out: dict = {"command": command, "note": note}
    if what and Path(what).expanduser().is_file():
        path = Path(what).expanduser()
        try:
            ref = code_ref(path) or {}
        except FileNotFoundError:  # git is not installed: the file is named by itself
            ref = {}
        out |= {"path": ref.get("path") or path.name, "repository": repo_url or ref.get("repository"), "commit": commit or ref.get("commit"),
                "url": None if commit or repo_url else ref.get("url")}  # a commit or repository named here makes its own link, below
        if ref and _dirty(path):
            out["uncommitted"] = True
    elif what and what.startswith(("http://", "https://")):
        out["url"] = what
    elif what:
        out |= {"path": what, "repository": repo_url, "commit": commit}
    if out.get("repository") and not out.get("url") and out.get("commit") and out.get("path"):
        base = web_url(out["repository"]) or out["repository"]
        out["url"] = f"{base}/blob/{out['commit']}/{out['path']}"
    name = (out.get("path") or out.get("url") or "").lower().split("?")[0]
    out["kind"] = kind or ("notebook" if name.endswith(".ipynb") else "script" if name.endswith(SCRIPTS) else "query" if command and not what else "other")
    return check({k: v for k, v in out.items() if v not in (None, "")})


def _dirty(path: Path) -> bool:
    found = subprocess.run(["git", "-C", str(path.parent), "status", "--porcelain", "--", path.name], capture_output=True, text=True)
    return found.returncode == 0 and bool(found.stdout.strip())
