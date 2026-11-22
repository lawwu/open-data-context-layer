# Changes, quality and alerts

What changed and how much it matters, whether the data passed its checks, and who is told.

## What changed, and how much it matters

`odcl diff` compares an entry between two points in its history, and a table's diff includes its columns, since a table's schema is its columns. Each change is labelled by how much it matters to what uses it:

| Severity | Changes |
| --- | --- |
| `breaking` | a column's type changed from one stated type to another; a table or column removed or retired |
| `semantic` | a definition, the inputs it is computed from, a foreign key, the copies of a definition, a privacy field, a document's text |
| `additive` | a column or table added, or revived |
| `cosmetic` | a description, owner, tags, layer, a type stated for the first time |

```sh
uv run odcl diff core.sales.daily_totals                 # its latest change, with its columns'
uv run odcl diff core.sales.daily_totals --since 7d      # or a revision (rev_...), or a time
```

What a project uses carries the worst change since it used it (`severity` on `get` of a project, `uses` and `overview`'s `changed_since_used`), measured from when the use was recorded. A used table counts as changed when one of its columns changed, even if the table's own revision did not. The same diff is `GET /v1/entries/{key}/diff?since=&until=`, the `what_changed` MCP tool and `Catalog.diff`, and an entry's page shows it, with "changes since" on each revision in its history.

## Data quality

odcl runs no tests. It keeps what your tests said, pinned to the revision of the table or column that was current when each ran, so "was this column failing when the model trained on it?" has an answer.

```sh
uv run odcl import --dbt-tests target/                       # run_results.json and manifest.json, after dbt test or dbt build
uv run odcl import --quality results.jsonl --source great_expectations
uv run odcl quality core.sales.daily_totals                  # the newest result of each test, on the table and its columns
uv run odcl quality core.sales.daily_totals --at 2026-11-01  # as they stood then
```

Anything that can write a line of JSON per result works, such as Great Expectations, Dataform assertions, Soda or a notebook:

```json
{"entry": "core.sales.daily_totals.amount", "test": "amount_not_negative", "status": "fail", "failures": 12, "observed_at": "2026-11-20T06:00:00Z", "run": "nightly-2026-11-20", "kind": "expression", "message": "12 rows below 0"}
```

- A status is `pass`, `warn`, `fail`, `error` or `skip`. A result is kept once per source, run, test and entry, so importing again changes nothing. An entry that is not registered is named in `unresolved`, not refused; a malformed line refuses the whole file.
- Each result pins the revision current when the test ran. A result from before the entry's latest revision says so (`current: false`).
- An entry's page and `get` show its newest results, a table with its columns'. `overview` lists `failing_and_used`: projects that use a column failing a test, or its table. A model version's trace lists `quality_at_training`: tests failing, when it was trained, on the columns its inputs came from and their tables.
- Over HTTP: `GET /v1/entries/{key}/quality?at=` and `POST /v1/quality` (importer). Run `odcl migrate` for migration 0013.

## Alerts

The overview lists what changed after a project used it; alerts tell the people behind the project. A subscription covers one project, the projects of one owner (their `owner` field), or every project, and sends to a webhook, a Slack incoming webhook or an email address, for changes of a severity and above (`semantic` by default):

```sh
uv run odcl alerts subscribe --project forecast --slack https://hooks.slack.com/services/…
uv run odcl alerts subscribe --owner ana --email ana@example.com --min-severity breaking
uv run odcl alerts subscribe --all --webhook https://ops.example.com/odcl
uv run odcl alerts send            # after imports, from the same scheduler; --dry-run to see what would go
```

- **What is sent.** What `odcl impact` lists: an entry a project uses that changed after the use (with each change and its severity, as `odcl diff` gives them), that is retired, or that is failing a data-quality test (it, or a column of a used table; told again when it fails anew after passing). A used table changes when its columns do. One message per subscription and run; the webhook's JSON has every change, with `project`, `key`, `kind` (`changed`, `retired` or `failing`), `severity` and `changes`.
- **Once each.** A delivery is recorded per subscription, project, entry and the entry's newest revision (a table's newest column counts), so a change is sent once, and a later change to the same entry is sent again. Recording use again (`odcl use`) clears it.
- **When it fails.** A failed delivery is tried again on each of the next two `send`s; after three failed tries in a row it waits an hour, then twice as long after each further failure, up to a day, and `send` reports the subscription as deferred until then. Until a message gets through, `odcl alerts list` and the Account page say the subscription is failing: since when, how many tries, the last error and the next try. A delivery still owed about something no longer there to tell (the entry changed again, the project used it again, the test passes) is dropped.
- **Who.** An editor subscribes and removes their own subscriptions; an admin sees and removes all of them. `send` needs an importer, like the imports it follows. A webhook address is `https://` (or `http://` on this machine); only its host is shown when subscriptions are listed, since its path or a `user:password@` is often the secret. Anyone with the database's credentials can read the addresses. When sending, a webhook on an internal address (loopback, private, link-local such as a cloud's metadata service) is refused unless its host is in `$ODCL_ALERT_ALLOW_HOSTS` (comma-separated), a redirect is a failed delivery rather than followed, and a failure says only that the webhook could not be reached or what status it answered.
- **Email** goes through `$ODCL_SMTP_HOST` (`$ODCL_SMTP_PORT`, default 587 with STARTTLS; `$ODCL_SMTP_USER`, `$ODCL_SMTP_PASSWORD`; `$ODCL_SMTP_FROM`). With `$ODCL_PUBLIC_URL`, each alert links to the entry's page.
- On the web page, an editor's Account page lists their subscriptions (an admin's, everyone's) and adds and removes them.
- Over HTTP: `GET`/`POST /v1/alerts/subscriptions`, `POST /v1/alerts/subscriptions/{id}/remove`, `POST /v1/alerts/send`. Run `odcl migrate` for migration 0012.
