# Changelog

Versions follow [semantic versioning](https://semver.org). While the version is `0.x`,
commands, the Python interface and the HTTP API may still change between minor versions,
and the entry says so when one does. The catalog's schema is migrated in place by
`odcl migrate`. The README's [Versioning and releases](README.md#versioning-and-releases)
section says how a release is cut.

## Unreleased

- Releases: a `vX.Y.Z` tag on `main` builds the sdist and wheel, attests them and publishes
  them as a GitHub release with this changelog's notes; a `latest` tag follows the newest
  release.
- Docs: a site on GitHub Pages, https://lawwu.github.io/open-data-context-layer/, built with
  MkDocs from the README, this changelog and `docs/`, and published on every push to `main`.
- Workflows: every action is locked to a commit in `.github/workflows/actions.lock`, checked
  on every pull request with zizmor.
