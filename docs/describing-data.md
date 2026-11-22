# Describing data

What people add on top of what is imported: terms, documents and privacy.

## Glossary terms and domains

A **term** is a word the business uses, with its definition: `net-revenue`, "revenue after refunds and discounts". A **domain** is a part of the business that owns data, projects and terms: `sales`. Both are entries, with revisions and history like any other, and both can be described by documents.

```sh
uv run odcl domain add sales --description "Selling, from quote to cash" --owner ana
uv run odcl term add net-revenue --definition "Revenue after refunds and discounts" --synonym "net sales" --domain sales --status approved
uv run odcl means core.sales.daily_totals.amount net-revenue      # what a table or column means; no terms clears them
uv run odcl in-domain core.sales.daily_totals sales               # a table, project or term; no domain takes it out
uv run odcl glossary [--domain sales]
```

- A column or table `means` terms; a table, project or term is `in_domain` one domain. A term's page lists what means it; a domain's page lists its tables, projects and terms; search finds a term by its synonyms. The web page has a Glossary.
- These links are said beside what importers say, so an import, merged or not, keeps them. A change to them is `cosmetic` in `odcl diff`.
- Over HTTP: `GET /v1/glossary`, and as an editor `POST /v1/terms`, `/v1/domains`, `/v1/entries/{key}/terms`, `/v1/entries/{key}/domain`. `find_data` searches terms and domains.

## Documents

A team's knowledge-base pages, findings and runbooks can say which data they describe, so a table or column shows the pages about it. A page says so in its frontmatter, and its body stays as it is:

```markdown
---
title: "Order status codes"
owner: Data Team
odcl:
  kind: reference                # reference, finding, runbook, glossary: free text, short
  describes:
    - key: column:warehouse.sales.orders.status_code
      section: status-codes      # optional: a heading anchor in this page
      says: "Closed = status_code IN ('C', 'X')"    # optional, one line
    - key: table:warehouse.ref.status_lookup
    - pattern: "column:*-prod.sales.orders.amount*" # a glob over entry keys
---
# Order status codes
…
```

```sh
uv sync --extra docs
uv run odcl import --docs ../knowledge-base/docs
uv run odcl docs warehouse.sales.orders.status_code
```

- **When another tool owns the frontmatter**, such as a linter that allows only its own keys, put the block in an HTML comment anywhere in the page instead (outside code blocks: one shown as an example in a fence is left alone). It holds what the `odcl:` key would, and is left out of the page's text:

  ```markdown
  <!-- odcl
  kind: finding
  describes:
    - key: column:warehouse.sales.orders.amount
  -->
  ```
- **Importing.** `import --docs` reads every `*.md` file with an `odcl:` block (or `<!-- odcl -->` comment) under the folders it is given and counts the others as skipped. A page becomes a `document` entry keyed `document:<repo>/<path>`, holding its title, owner, kind, text, headings with their anchors, commit, web address and content digest.
- **Commit and web address.** They come from git when the page is in a checkout, else from `--commit` and `--repo-url`.
- **Revisions.** Importing an unchanged page changes nothing; an edited one is a new revision.
- **Search** covers the title, headings and text.
- **`describes` links.** Each one pins the revision of the table, column or project it names. Re-importing an unchanged page keeps that pin, because nobody re-read the page. So when the column is redefined later, its page, the column and `odcl overview` all say it was redefined after the page described it, until the page is edited.
- **A column** also shows the pages that describe its table.
- **Patterns** are resolved at every import, one link per entry they match, and kept on the page, so a later import links copies added since.
- **What is not found.** A key or pattern that matches nothing, or a section the page has no heading for, is not an error. It is reported by the import and on the page.
- **Retirement.** A page imported before and now gone from the folder, or that no longer has an `odcl:` block, is retired: a new revision that describes nothing. Its links stay in its history, not as current. Importing one file never retires the others.
- **One transaction.** Every file of one import is written together.

The web page has a page for each document, with its links grouped by section and its text shown as text; nothing in a page runs. Every entry has a "Documented in" panel, and search finds documents. The MCP server has a `documents_for` tool, and `find_data` returns documents. `Catalog.put("document", …)` and `odcl.docs.import_docs` are the Python interface.

**A model version can rely on a document**, such as a finding about how a field behaves. The training manifest lists it under `relied_on` (a document key, or `{"document", "digest" or "revision", "why"}`), or `odcl rely MODEL --version V DOCUMENT… --why TEXT` records it afterwards. Each is pinned at one revision: the one the manifest names or whose content digest it gives, else the one current when the training task ended. When the page is corrected later, `odcl explain model`, the model and document pages and `odcl overview` say which model versions used the old text.

## Privacy

Tables and columns can say whether they hold personal data (`pii`), how it is scrubbed (`scrubbing`: hashed, truncated, removed…), their data category (`data_category`) and how long they are kept (`retention`). A column that names no category or retention of its own takes its table's. Only what is said is kept: a column that says nothing about `pii` is unknown, not "no".

```sh
uv run odcl privacy set warehouse.crm.customers --data-category contact --retention 400d
uv run odcl privacy set warehouse.crm.customers.email --pii yes --scrubbing hashed
uv run odcl privacy export --pii-only > privacy.csv        # or --json; --table for one table
```

`odcl add table|column` take the same flags. A dbt project can say them in `meta` (directly, or under `meta.privacy`) on a source, a model or a column. Imports keep what was set, including a dbt import, which says everything else about its models; what dbt's `meta` says replaces it. The web page shows them on each entry, and `GET /v1/privacy` answers what `privacy export` does.

**Proposing privacy fields.** `odcl privacy propose` suggests `pii` and a `data_category` for columns that say nothing about `pii` yet, from the words in each column's name, the glossary terms it means and its description, with the reason and a confidence (`high`: `email`, `date_of_birth`, `passport`; `medium`: an address, an IP, `customer_id`, or `name` on a table of people; `low`: `name` elsewhere, or a term or description that mentions such a thing). It reads no rows, so it cannot tell a column of people's names from one of product names: that is what the review is for. Nothing is written until a person accepts:

```sh
uv run odcl privacy propose --min-confidence medium --write review.jsonl   # set "accept": true on the right lines, fix what is wrong
uv run odcl privacy apply review.jsonl                                     # records the accepted lines, all or none
```

`--all` also proposes for columns that already say, `--table` limits it to one table, and `GET /v1/privacy/proposals` returns the same. A table's page lists its columns that may hold personal data.
