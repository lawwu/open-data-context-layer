# Staffing forecast trace

Synthetic, sanitized fixtures for the first model trace in [the implementation plan](../../../docs/implementation-plan.md): `odcl explain model staffing-forecast --version v17`. Every identifier is made up. Each file has the shape of the Google response it stands in for, cut down to the fields the trace reads.

| File | Stands in for |
|---|---|
| `bigquery_tables.json` | BigQuery `tables.get` for the raw table, the feature table and its snapshot |
| `bigquery_jobs.json` | Job resources as BigQuery `jobs.list` returns them with `projection=full` |
| `dataform_compilation_result.json` | Dataform `compilationResults.get` and the actions from `compilationResults.query` |
| `dataform_workflow_invocation.json` | Dataform `workflowInvocations.get` and the actions from `workflowInvocations.query` |
| `vertex_pipeline_job.json` | Vertex AI `pipelineJobs.get` |
| `training_manifest.json` | The manifest the training component writes after saving the artifact |
| `expected_explain.json` | What `explain_model("staffing-forecast", "v17")` should answer |

`tests/test_dataform.py` reads the BigQuery and Dataform files and checks the dataset half of `expected_explain.json`. The test asserting the whole model trace lands with the code that first satisfies it (change 3 in the plan).
