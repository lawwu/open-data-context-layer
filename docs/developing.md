# Developing and releases

Running the tests, cutting a release, and what is not built yet.

## Developing

Tests run against a real PostgreSQL database. Each test creates its own schema in it and drops it afterwards.

```sh
createdb odcl_test
ODCL_TEST_DATABASE_URL=postgresql://localhost/odcl_test uv run pytest -q
```

Without `ODCL_TEST_DATABASE_URL` the database tests are skipped locally and fail in CI. Schema changes go in a new numbered file in `src/odcl/migrations/`.

**Adding a field.** What an entry can say is one table, `FIELDS` in [`src/odcl/fields.py`](../src/odcl/fields.py): each field's name, the kinds of entry it belongs to, how it is checked and stored (in the revision's body, or as links of a relation), and whether it is said `beside` what importers say, so that an import which says the whole entry keeps it. `Catalog.put` reads the table, so a new field is one entry there, and `tests/test_fields.py` checks every field of every kind is kept and cleared as the table says. The order of the fields with links is the order of an entry's links, and part of what a revision says: add new ones after the existing ones.

**Impact.** `odcl.impact.impact(catalog, project=None)` is the one read of what projects are exposed to through what they use: an item per project, used entry and kind (`changed`, `retired`, `failing`), with its severity, changes or failing tests, the project's owner, and a `state` that moves when the exposure does. The overview's three lists, a project's `uses`, the project list's counts and alerts all come from it, so a new kind of exposure reaches all of them.

**Reading history.** `catalog.history` ([`src/odcl/history.py`](../src/odcl/history.py)) is the one place that knows how entries, revisions and their pinned links are stored: an entry's head, its revisions in order, the revision that was its head at a time, a point in the order of all revisions, what a revision pinned, and when a link began. Modules that ask about the past (diffs, alerts, quality results, exports, model traces) ask it rather than the tables.

**Adding an operation.** Every command, HTTP route and MCP tool comes from one table, `OPERATIONS` in [`src/odcl/operations.py`](../src/odcl/operations.py). An operation says what it runs (`run(catalog, **params)`, returning plain data), the params it takes, the role the HTTP API requires, and where it appears: `cli="privacy set"`, `http="POST /v1/privacy"`, `mcp="tool_name"`, any of them. Each `Param` says how each adapter spells it (a positional argument or a flag, a query or body field, an MCP argument), and `text(result, params)` is what a person reads on the command line. `cli.py`, `server.py` and `mcp.py` build their commands, routes and tools from the table, so a new operation is one entry there. `tests/test_operations.py` checks every entry: its params are what `run` takes, each adapter offers exactly what the table says, and the command line, the HTTP API and MCP answer a read alike.

**The docs site**, https://lawwu.github.io/open-data-context-layer/, is drawn from the README, [`CHANGELOG.md`](../CHANGELOG.md) and the pages in `docs/` with [Zensical](https://zensical.org) ([`zensical.toml`](../zensical.toml)), so there is no second copy to keep in step. [`scripts/docs.py`](../scripts/docs.py) assembles those files into `build/docs/` first, pointing links written for GitHub at the site's pages, or at the file on GitHub when the site has none. Every push to `main` publishes it (`.github/workflows/pages.yml`); pull requests build it in strict mode, so a broken link or anchor fails there. A new page in `docs/` goes in `nav` in `zensical.toml`.

```sh
uv run --only-group site python scripts/docs.py serve   # then http://127.0.0.1:8000/; restart after editing
uv run --only-group site python scripts/docs.py build   # strict, into site/
```

**The workflows** (`.github/workflows/`): `ci.yml` runs ruff and the tests on PostgreSQL 16 and merges Dependabot's minor and patch updates once they pass; `pages.yml` builds and publishes the docs; `release.yml` and `latest.yml` cut releases (below); `actions.yml` checks the workflows themselves. They name exact action versions, and `.github/workflows/actions.lock` records each one's commit, so a release tag moved to other code later can't change what runs. After changing a workflow, relock with GitHub's [gh-actions-lock](https://github.com/github/gh-actions-lock) and check it with [zizmor](https://docs.zizmor.sh), as CI does:

```sh
gh extension install github/gh-actions-lock   # once
gh actions-lock                               # pin new or changed actions
uvx zizmor .github/                           # security lint for the workflows
```

Relocking rewrites each `uses:` line it pins and drops a comment at the end of that line, so keep comments (zizmor's `# zizmor: ignore[...]` among them) on lines of their own. Dependabot's weekly update of the actions updates the lock file too.

## Versioning and releases

Releases are git tags, `vX.Y.Z`, following semantic versioning; [`CHANGELOG.md`](../CHANGELOG.md) says what each one changed. odcl is not on PyPI (the `bigquery` extra depends on Sounding by git URL, which PyPI refuses), so install a release from GitHub, a specific one with `@vX.Y.Z` or the newest with `@latest`:

```sh
uv tool install "open-data-context-layer[mcp] @ git+https://github.com/lawwu/open-data-context-layer@latest"
```

Without the `@…` you get the latest `main`, which takes Dependabot's updates as soon as they merge, before any release. Each release is also a Docker image (see [In Docker](serving.md#in-docker)). Each release on GitHub also carries the sdist and wheel, with [build provenance](https://docs.github.com/actions/security-for-github-actions/using-artifact-attestations) (`gh attestation verify FILE -R lawwu/open-data-context-layer`).

To cut a release:

1. On a branch, move the changelog's "Unreleased" notes under a new `## X.Y.Z (YYYY-MM-DD)` heading and leave "Unreleased" empty.
2. Bump the version, which updates `pyproject.toml` and `uv.lock`, and set the same version in `src/odcl/__init__.py` (CI fails while the two disagree):

   ```sh
   uv version --bump minor   # or patch / major
   ```

3. Open a pull request titled `Release X.Y.Z: …` and merge it once CI passes.
4. Tag the merge commit on `main` and push the tag:

   ```sh
   git switch main && git pull
   git tag -a vX.Y.Z -m "X.Y.Z"
   git push origin vX.Y.Z
   ```

The tag starts `release.yml`, which refuses a tag that isn't on `main`, whose version doesn't match `pyproject.toml` and `src/odcl/__init__.py`, or that has no changelog section. It builds the sdist and wheel, checks the wheel installs and runs, attests both, and creates the GitHub release with the changelog section as its notes (a release already made by hand keeps its title and notes and gets the files). `docker.yml` builds the image for both architectures, starts each against PostgreSQL (`scripts/docker-smoke.sh`), and publishes it; run by hand with a tag, it publishes an older release's image. `latest.yml` moves the `latest` tag to the highest `vX.Y.Z`, so a patch to an older line doesn't move it back. Both can also be run by hand from the Actions tab.

## Not built yet

- The machine-learning shape on top: training and serving features, labels, training datasets and model cards.
- Proposing manifests for pipelines laid out differently from a trainer with a Model output and a `metadata.json` beside it.
- Sending the catalog's runs and lineage out as OpenLineage events.
- Discovering Dataform invocations, and Dataform runs started from the CLI rather than the managed service.
- Permissions finer than a role, such as who may change which tables. Whoever holds the database's credentials can still do anything.
- Retiring what an OpenLineage producer stopped emitting: events say what a run touched, not everything that exists.

The distribution is `open-data-context-layer`, the Python package is `odcl`, and the command is `odcl`. The repository is [lawwu/open-data-context-layer](https://github.com/lawwu/open-data-context-layer).
