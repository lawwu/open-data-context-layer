# Live trace test

Runs the first model trace for real, in a sandbox Google Cloud project you own: a Dataform build, a snapshot handoff, and a Vertex AI training run. It then traces the model version back to its raw column with `odcl`. The [fixture](../../tests/fixtures/staffing_trace/) is the same example with made-up identifiers.

**Cost.** The BigQuery work reads and writes a few kilobytes (BigQuery bills at least 10 MB per query). The pipeline runs one small training container for a few minutes.

**1. Set up, once.** This creates two service accounts and grants them narrow roles. Those are security changes, so read the script and run it yourself:

```sh
export ODCL_LIVE_PROJECT=my-sandbox-project
bash examples/live_trace/setup.sh
```

| Created | Why |
|---|---|
| `odcl-dataform-test` service account | Dataform runs as it. It may run BigQuery jobs, read `odcl_demo_raw` and write `odcl_demo_features`. Dataform's own service agent may act as it. |
| `odcl-vertex-test` service account | The training step runs as it. It is a Vertex AI user, may read `odcl_demo_frozen`, and may write the bucket. |
| `gs://PROJECT-odcl-live` bucket | The pipeline root, the model and its manifest |
| `odcl_demo_raw`, `odcl_demo_features`, `odcl_demo_frozen` datasets | Synthetic raw data, the built table and its snapshots |

**2. Build and hand off.** `run.py dataform` writes 20 synthetic rows, runs the Dataform project in [`dataform/`](dataform/) and snapshots the table it built. It prints the `odcl` commands that import and explain the result.

```sh
uv run --all-extras python examples/live_trace/run.py dataform
```

**3. Train.** `run.py vertex` runs a one-step pipeline that reads the snapshot, fits a trivial model, uploads it and writes the training manifest. It prints the commands that import and explain the model version.

```sh
uv run --all-extras --with "kfp>=2.7,<3" python examples/live_trace/run.py vertex PROJECT.odcl_demo_frozen.shift_inputs_BUILD@TIME v1
```

**What a first run can trip on.**

- A permission granted moments ago can take a few minutes to apply. If the build fails because Dataform's service agent "does not have permission to generate tokens", wait and run step 2 again.
- `odcl` reads Google through your Application Default Credentials, and those bill each request to their quota project. If that project has no billing, Cloud Storage refuses with "The billing account for the owning project is disabled". Point the requests at the sandbox for the session with `export GOOGLE_CLOUD_QUOTA_PROJECT=$ODCL_LIVE_PROJECT`.
- A build compiled from a Dataform workspace has no git commit. The trace says so under `missing`, because the definition's file cannot be pinned to a version. Connect the repository to git and compile from a branch to close that gap.

**4. Clean up.**

```sh
bash examples/live_trace/cleanup.sh
```
