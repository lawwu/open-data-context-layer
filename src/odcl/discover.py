"""Finding model versions in Vertex AI Pipelines without a hand-built manifest.

`runs` lists a project's pipeline runs and says which already have a model version in the catalog. `propose` reads
one run and drafts its training manifest: the trainer task (the one with a Model output) and its model file, the
`metadata.json` the trainer wrote beside it (`feature_columns`, the label, and the version and commit when it says
them), and the data loader's output, which is the dataset version the model read. Every value says where it came
from, and anything that could not be found or had to be guessed is listed for review. Nothing is written: a person
reads the draft, completes it, and imports it with `odcl import --vertex-manifest`, which checks every claim.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from .manifest import SCHEMA_VERSION
from .store import Catalog, CatalogError, require, stamp
from .vertex import JOB, MAX_HASHED, SUCCEEDED, _time, task_detail

MODEL_SCHEMA = "system.Model"
DATASET_SCHEMA = "system.Dataset"
LABELS = ("label_column", "label", "target_column", "target")


def runs(catalog: Catalog, client, project: str, since: datetime) -> list[dict]:
    """The pipeline runs created since `since`, newest first, each with whether it produced a model version the
    catalog has recorded."""
    recorded: dict[str, list[str]] = {}
    for r in catalog.db.execute("""SELECT t.evidence ->> 'pipeline_run' AS run, b.project_key, b.version FROM model_builds b
                                   JOIN executions t ON t.id = b.producing_execution ORDER BY b.version"""):
        recorded.setdefault(r["run"], []).append(f"{r['project_key'].removeprefix('project:')} {r['version']}")
    return [{"pipeline_run": j["name"], "display_name": j.get("displayName"), "state": j.get("state"),
             "created": stamp(_time(j["createTime"])) if j.get("createTime") else None, "ended": stamp(_time(j["endTime"])) if j.get("endTime") else None,
             "model_versions": recorded.get(j["name"], [])} for j in client.pipeline_jobs(project, since)]


def _artifacts(task: dict) -> list[tuple[str, dict]]:
    return [(name, a) for name, out in (task.get("outputs") or {}).items() for a in out.get("artifacts") or []]


def _pick(tasks: list[dict], named: str | None, test, what: str, flag: str, review: list[str]) -> dict | None:
    if named:
        return next(t for t in tasks if t.get("taskName") == named)
    found = [t for t in tasks if test(t)]
    if len(found) > 1:
        review.append(f"Several tasks could be the {what} ({', '.join(t['taskName'] for t in found)}); name one with {flag}")
        return None
    if not found:
        review.append(f"No task looks like the {what}; name it with {flag}")
    return found[0] if found else None


def propose(catalog: Catalog, client, pipeline_run: str, *, trainer: str | None = None, loader: str | None = None, loader_output: str | None = None,
            model: str | None = None, version: str | None = None) -> dict:
    """A draft training manifest for one pipeline run: {"manifest", "sources", "review", "ready"}."""
    require(JOB.match(pipeline_run or ""), "pipeline_run", "Name a Vertex AI pipeline job: projects/P/locations/L/pipelineJobs/J")
    job = client.get(pipeline_run)
    tasks = (job.get("jobDetail") or {}).get("taskDetails") or []
    for named in (trainer, loader):
        if named:
            task_detail(job, pipeline_run, named)  # refused when the run has no such task
    review: list[str] = []
    sources: dict[str, str] = {"pipeline_run": "given"}
    manifest: dict = {"schema_version": SCHEMA_VERSION, "model": None, "version": None, "pipeline_run": pipeline_run, "task": None, "execution": None,
                      "training_commit": None, "prediction_cutoff": None, "dataset_version": None, "inputs": [], "artifact": None}

    # the trainer, its model file and the metadata.json beside it
    train = _pick(tasks, trainer, lambda t: any(a.get("schemaTitle") == MODEL_SCHEMA or name == "model" for name, a in _artifacts(t)),
                  "trainer", "--trainer", review)
    metadata: dict = {}
    if train:
        manifest["task"], manifest["execution"] = train["taskName"], (train.get("execution") or {}).get("name")
        sources["task"] = "given" if trainer else "the task with a Model output"
        if train.get("state") not in SUCCEEDED:
            review.append(f"Task {train['taskName']} is {train.get('state')}: only a task that succeeded produced a model")
        files = [o for _, a in _artifacts(train) if a.get("uri", "").startswith("gs://") for o in _objects(client, a["uri"])]
        meta = [o for o in files if o["uri"].endswith("/metadata.json") or o["uri"].endswith("metadata.json")]
        if meta:
            try:
                metadata = json.loads(client.read(meta[0]["uri"], meta[0]["generation"]))
                sources["inputs"] = meta[0]["uri"]
            except (ValueError, CatalogError):
                review.append(f"{meta[0]['uri']} is not JSON")
        else:
            review.append(f"Task {train['taskName']} wrote no metadata.json, so its inputs must be listed by hand")
        models = [o for name, a in _artifacts(train) if a.get("schemaTitle") == MODEL_SCHEMA or name == "model"
                  for o in _objects(client, a["uri"]) if not o["uri"].endswith("metadata.json")]
        if len(models) == 1:
            o = models[0]
            manifest["artifact"] = {"uri": o["uri"], "generation": o["generation"], "sha256": None}
            sources["artifact"] = "the trainer's Model output"
            if int(o.get("size") or 0) <= MAX_HASHED:
                manifest["artifact"]["sha256"] = hashlib.sha256(client.read(o["uri"], o["generation"])).hexdigest()
            else:
                review.append(f"{o['uri']} is larger than {MAX_HASHED >> 20} MiB: give its sha256")
        else:
            review.append("The trainer's Model output holds " + (f"{len(models)} files ({', '.join(o['uri'] for o in models[:5])})" if models else "no file")
                          + ": fill in artifact.uri, generation and sha256")

    if isinstance(metadata, dict):
        features = metadata.get("feature_columns") or metadata.get("features") or []
        manifest["inputs"] = [{"field": str(f), "role": "feature", "position": n} for n, f in enumerate(features)]
        label = next((metadata[k] for k in LABELS if isinstance(metadata.get(k), str)), None)
        if label:
            manifest["inputs"].append({"field": label, "role": "label"})
        for field, keys in (("model", ("model", "model_name")), ("version", ("version", "model_version")),
                            ("training_commit", ("training_commit", "git_commit", "commit")), ("prediction_cutoff", ("prediction_cutoff", "cutoff"))):
            said = next((str(metadata[k]) for k in keys if metadata.get(k)), None)
            if said:
                manifest[field], sources[field] = said, "metadata.json"
    if not manifest["inputs"]:
        review.append("No feature_columns were found: list the fields the model was trained on, each with its role")
    if model:
        manifest["model"], sources["model"] = model, "given"
    if version:
        manifest["version"], sources["version"] = version, "given"
    if not manifest["model"]:
        review.append("Name the model with --model, or have the trainer write model_name in metadata.json")
    if not manifest["version"]:
        manifest["version"], sources["version"] = pipeline_run.rsplit("/", 1)[1], "the pipeline job's id: check it is the version you mean"
        review.append("The version is the pipeline job's id; give --version if the model is versioned otherwise")
    if not manifest["training_commit"]:
        review.append("No training commit was found: fill in training_commit, or leave it null")

    # the data the trainer read: the loader's output, as `odcl record dataset-files` names it
    load = _pick(tasks, loader, lambda t: t is not train and any(a.get("schemaTitle") == DATASET_SCHEMA or name.startswith("output_")
                                                                 for name, a in _artifacts(t)), "data loader", "--loader", review)
    if load:
        outputs = [name for name, _ in _artifacts(load)]
        name = loader_output or next((o for o in outputs if "train" in o), outputs[0] if outputs else None)
        uri = next((a["uri"] for n, a in _artifacts(load) if n == name), None)
        if uri:
            reference = f"{uri.rstrip('/')}@{pipeline_run.rsplit('/', 1)[1]}"
            manifest["dataset_version"], sources["dataset_version"] = reference, f"task {load['taskName']}, output {name}"
            if not catalog.db.execute("SELECT 1 FROM dataset_versions WHERE reference = %s", (reference,)).fetchone():
                review.append(f"Record the files first: odcl record dataset-files --pipeline-run {pipeline_run} --task {load['taskName']} --output {name} "
                              "--find-export-job")
        else:
            review.append(f"Task {load['taskName']} has no output {name}; its outputs are {', '.join(outputs) or 'none'}. Name one with --loader-output")
    missing = [k for k in ("model", "version", "task", "dataset_version", "artifact") if not manifest[k]] + ([] if manifest["inputs"] else ["inputs"])
    if manifest["artifact"] and not manifest["artifact"]["sha256"]:
        missing.append("artifact.sha256")
    return {"manifest": manifest, "sources": sources, "review": review, "missing": missing, "ready": not missing and not review}


def _objects(client, uri: str) -> list[dict]:
    """The objects at `uri`: the object itself, or every object under it as a folder."""
    one = client.object(uri, missing_ok=True)
    if one is not None:
        return [{"uri": uri, "generation": str(one.get("generation")), "size": one.get("size")}]
    return [{"uri": f"gs://{o['bucket']}/{o['name']}", "generation": str(o["generation"]), "size": o.get("size")}
            for o in client.list(uri.rstrip("/") + "/") if not o["name"].endswith("/")]
