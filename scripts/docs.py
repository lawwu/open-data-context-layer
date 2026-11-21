"""Build or preview the docs site (zensical.toml) with Zensical.

    uv run --only-group site python scripts/docs.py build   # -> site/, strict
    uv run --only-group site python scripts/docs.py serve   # -> http://127.0.0.1:8000/

The site is drawn from the repository's own Markdown rather than kept as a second copy:
README.md becomes the home page and CHANGELOG.md the changelog, next to the pages in docs/.
Zensical has no hooks, so this first assembles those pages into build/docs/ (the docs_dir
in zensical.toml, not committed). Links in them are written for GitHub, relative to where
each file sits in the repository; here they are pointed at the matching page of the site,
or at the file on GitHub when the site has no page for it. `serve` reads what was assembled
when it started: after editing README.md, CHANGELOG.md or docs/, start it again.
"""

from __future__ import annotations

import posixpath
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "build" / "docs"
GITHUB = "https://github.com/lawwu/open-data-context-layer"
LINK = re.compile(r"(\]\()([^)\s]+)(\))")
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:")


def pages() -> dict[str, str]:
    """Each page of the site (its path under build/docs/) and the repository file it comes from."""
    found = {"index.md": "README.md", "changelog.md": "CHANGELOG.md"}
    for path in sorted((ROOT / "docs").rglob("*.md")):
        found[path.relative_to(ROOT / "docs").as_posix()] = path.relative_to(ROOT).as_posix()
    return found


def rewrite(markdown: str, page: str, source: str, site: dict[str, str]) -> str:
    """Point a page's repository-relative links at the site, or at GitHub."""
    by_source = {src: dst for dst, src in site.items()}

    def link(m: re.Match) -> str:
        target = m.group(2)
        if SCHEME.match(target) or target.startswith(("#", "/")):
            return m.group(0)
        path, _, anchor = target.partition("#")
        repo_path = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
        if repo_path in by_source:
            new = posixpath.relpath(by_source[repo_path], posixpath.dirname(page) or ".")
        else:
            kind = "tree" if path.endswith("/") or (ROOT / repo_path).is_dir() else "blob"
            new = f"{GITHUB}/{kind}/main/{repo_path}"
        return f"{m.group(1)}{new}{'#' + anchor if anchor else ''}{m.group(3)}"

    return LINK.sub(link, markdown)


def assemble() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    site = pages()
    for page, source in site.items():
        dest = OUT / page
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rewrite((ROOT / source).read_text(encoding="utf-8"), page, source, site), encoding="utf-8")


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "build"
    if command not in ("build", "serve"):
        print(__doc__, file=sys.stderr)
        return 2
    assemble()
    args = ["zensical", "build", "--clean", "--strict"] if command == "build" else ["zensical", "serve", *argv[1:]]
    return subprocess.call(args, cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
