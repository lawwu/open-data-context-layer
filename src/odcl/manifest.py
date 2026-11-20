"""The training manifest: what a training task writes after saving its model, so the catalog can tell which data
and which fields produced it. It has no dependencies, so training code can copy or vendor this one file.

The manifest is a claim by the trainer. `odcl import --vertex-manifest` checks it against Vertex AI, Cloud
Storage and the catalog before recording anything.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

SCHEMA_VERSION = 1
ROLES = ("feature", "label", "cohort_filter", "join_key", "weight")


def sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(*, model: str, version: str, pipeline_run: str, task: str, training_commit: str | None, prediction_cutoff: str | None,
          dataset_version: str, inputs: list[dict], artifact_uri: str, artifact_path, generation: str, execution: str | None = None) -> dict:
    """A manifest for an artifact already uploaded to `artifact_uri` (at object `generation`) from `artifact_path`.

    `inputs` are the fields of the training matrix as the code used them, each {"field", "role", "position"},
    built from the matrix at fit time rather than from configuration. `dataset_version` is the identifier the
    build handed to training, project.dataset.snapshot@time."""
    for item in inputs:
        if item.get("role") not in ROLES or not item.get("field"):
            raise ValueError(f"Each input needs a field and a role, one of {', '.join(ROLES)}: {item}")
    return {"schema_version": SCHEMA_VERSION, "model": model, "version": version, "pipeline_run": pipeline_run, "task": task,
            **({"execution": execution} if execution else {}), "training_commit": training_commit, "prediction_cutoff": prediction_cutoff,
            "dataset_version": dataset_version,
            "inputs": [{"field": i["field"], "role": i["role"], **({"position": i["position"]} if i.get("position") is not None else {})} for i in inputs],
            "artifact": {"uri": artifact_uri, "generation": str(generation), "sha256": sha256(Path(artifact_path))}}
