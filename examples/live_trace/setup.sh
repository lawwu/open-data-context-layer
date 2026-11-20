#!/usr/bin/env bash
# One-time setup for the live trace test, in a project you own. Run it yourself: it creates two service accounts
# and grants them narrow roles, which are security changes. Everything it creates is listed in cleanup.sh.
#
#   ODCL_LIVE_PROJECT=my-sandbox-project bash examples/live_trace/setup.sh
set -euo pipefail
trap 'echo "setup.sh stopped at line $LINENO; fix the error above and run it again (every step is safe to repeat)" >&2' ERR
P=${ODCL_LIVE_PROJECT:?Set ODCL_LIVE_PROJECT to a sandbox project you own}
REGION=${ODCL_LIVE_REGION:-us-central1}
NUMBER=$(gcloud projects describe "$P" --format='value(projectNumber)')
DF="odcl-dataform-test@$P.iam.gserviceaccount.com"
VX="odcl-vertex-test@$P.iam.gserviceaccount.com"
BUCKET="gs://$P-odcl-live"

echo "== APIs"
gcloud services enable dataform.googleapis.com aiplatform.googleapis.com bigquery.googleapis.com storage.googleapis.com --project "$P"

echo "== Service accounts"
for name in odcl-dataform-test odcl-vertex-test; do
  gcloud iam service-accounts describe "$name@$P.iam.gserviceaccount.com" --project "$P" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "$name" --project "$P" --display-name "odcl live trace test"
done

echo "== Dataform runs as $DF"
# Dataform's own service agent usually exists once the API is on. Only create it if it is missing, and never prompt:
# `gcloud beta` asks to install its component, which would wait silently for an answer.
if ! gcloud projects get-iam-policy "$P" --format=json | grep -q "service-$NUMBER@gcp-sa-dataform"; then
  CLOUDSDK_CORE_DISABLE_PROMPTS=1 gcloud beta services identity create --service dataform.googleapis.com --project "$P" </dev/null >/dev/null 2>&1 \
    || echo "  could not create Dataform's service agent; run once: gcloud beta services identity create --service dataform.googleapis.com --project $P" >&2
fi
# A service account created a moment ago can take a minute or two before IAM accepts grants on it.
for attempt in 1 2 3 4 5 6 7 8; do
  gcloud iam service-accounts add-iam-policy-binding "$DF" --project "$P" --condition=None --quiet \
    --member "serviceAccount:service-$NUMBER@gcp-sa-dataform.iam.gserviceaccount.com" --role roles/iam.serviceAccountTokenCreator >/dev/null && break
  [ "$attempt" = 8 ] && false
  echo "  not accepted yet; retrying in 15s" >&2; sleep 15
done
gcloud projects add-iam-policy-binding "$P" --condition=None --quiet --member "serviceAccount:$DF" --role roles/bigquery.jobUser >/dev/null

echo "== Vertex AI training runs as $VX"
gcloud projects add-iam-policy-binding "$P" --condition=None --quiet --member "serviceAccount:$VX" --role roles/aiplatform.user >/dev/null
gcloud storage buckets describe "$BUCKET" >/dev/null 2>&1 \
  || gcloud storage buckets create "$BUCKET" --project "$P" --location "$REGION" --uniform-bucket-level-access
gcloud storage buckets add-iam-policy-binding "$BUCKET" --member "serviceAccount:$VX" --role roles/storage.objectAdmin >/dev/null

echo "== Datasets, each readable or writable only by the account that needs it"
python3 - "$P" "$DF" "$VX" <<'PY'
import json, subprocess, sys
project, dataform, vertex = sys.argv[1:]
grants = {"odcl_demo_raw": [(dataform, "READER")], "odcl_demo_features": [(dataform, "WRITER")], "odcl_demo_frozen": [(vertex, "READER")]}
for dataset, members in grants.items():
    ref = f"{project}:{dataset}"
    if subprocess.run(["bq", "show", ref], capture_output=True).returncode:
        subprocess.run(["bq", "--location=US", "mk", "--dataset", "--description", "odcl live trace test; safe to delete", ref], check=True)
    info = json.loads(subprocess.run(["bq", "show", "--format=prettyjson", ref], check=True, capture_output=True, text=True).stdout)
    access = info.get("access", [])
    for email, role in members:
        if not any(a.get("userByEmail") == email and a.get("role") == role for a in access):
            access.append({"role": role, "userByEmail": email})
    path = f"/tmp/odcl-access-{dataset}.json"
    with open(path, "w") as f:
        json.dump({"access": access}, f)
    subprocess.run(["bq", "update", "--source", path, ref], check=True, capture_output=True)
    print(f"{dataset}: " + ", ".join(f"{role.lower()} for {email}" for email, role in members))
PY
echo "Done. Next: ODCL_LIVE_PROJECT=$P python examples/live_trace/run.py dataform"
