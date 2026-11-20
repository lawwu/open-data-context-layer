# dbt medallion fixture

A small dbt project with bronze sources, silver staging views and a gold feature table. `target/manifest.json` and
`target/catalog.json` are real dbt output (dbt 1.12 with dbt-duckdb), reduced to the keys the catalog importer reads.

To regenerate: create `bronze.accounts` and `bronze.tickets` in `churn.duckdb`, then run
`dbt run --profiles-dir .` and `dbt docs generate --profiles-dir .` here, and reduce the two files the same way.
