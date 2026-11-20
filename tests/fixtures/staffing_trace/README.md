# Staffing forecast trace

Synthetic, sanitized fixtures for the first model trace in [the implementation plan](../../../docs/implementation-plan.md): `odcl explain model staffing-forecast --version v17`. Every identifier is made up. Each file has the shape of the Google response it stands in for, cut down to the fields the trace reads.

| File | Stands in for |
|---|---|
| `bigquery_tables.json` | BigQuery `tables.get` for the raw table, the feature table and its snapshot |
| `bigquery_jobs.json` | Rows of the regional `INFORMATION_SCHEMA.JOBS_BY_PROJECT` view |
| `dataform_compilation_result.json` | Dataform `compilationResults.get` and the actions from `compilationResults.query` |
| `dataform_workflow_invocation.json` | Dataform `workflowInvocations.get` and the actions from `workflowInvocations.query` |
| `vertex_pipeline_job.json` | Vertex AI `pipelineJobs.get` |
| `training_manifest.json` | The manifest the training component writes after saving the artifact |
| `expected_explain.json` | What `explain_model("staffing-forecast", "v17")` should answer |

No test reads these yet. The test asserting the trace lands with the code that first satisfies it (change 3 in the plan).
