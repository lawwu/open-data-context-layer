"""MkDocs hooks for the docs site (mkdocs.yml).

The site is drawn from the repository's own Markdown: README.md becomes the home page and
CHANGELOG.md the changelog, next to the pages in docs/. Links in those files are written
for GitHub, relative to where each file sits in the repository; here they are pointed at
the matching page of the site, or at the file on GitHub when the site has no page for it.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from mkdocs.structure.files import File

ROOT = Path(__file__).resolve().parent.parent
GITHUB = "https://github.com/lawwu/open-data-context-layer"

# Site page -> the repository file it comes from, for the pages added here.
GENERATED = {"index.md": "README.md", "changelog.md": "CHANGELOG.md"}
LINK = re.compile(r"(\]\()([^)\s]+)(\))")


def on_files(files, config):
    for src_uri, repo_path in GENERATED.items():
        files.append(File.generated(config, src_uri, content=(ROOT / repo_path).read_text(encoding="utf-8")))
    return files


def _repo_path(page_src_uri: str) -> str:
    return GENERATED.get(page_src_uri) or posixpath.join("docs", page_src_uri)


def _site_page(repo_path: str) -> str | None:
    for src_uri, source in GENERATED.items():
        if source == repo_path:
            return src_uri
    if repo_path.startswith("docs/") and repo_path.endswith(".md"):
        return repo_path[len("docs/"):]
    return None


def on_page_markdown(markdown, page, config, files):
    here = _repo_path(page.file.src_uri)

    def rewrite(m: re.Match) -> str:
        target = m.group(2)
        if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith(("#", "/")):
            return m.group(0)
        path, _, anchor = target.partition("#")
        repo_path = posixpath.normpath(posixpath.join(posixpath.dirname(here), path))
        page_uri = _site_page(repo_path)
        if page_uri is not None:
            new = posixpath.relpath(page_uri, posixpath.dirname(page.file.src_uri) or ".")
        else:
            kind = "tree" if path.endswith("/") or (ROOT / repo_path).is_dir() else "blob"
            new = f"{GITHUB}/{kind}/main/{repo_path}"
        return f"{m.group(1)}{new}{'#' + anchor if anchor else ''}{m.group(3)}"

    return LINK.sub(rewrite, markdown)
