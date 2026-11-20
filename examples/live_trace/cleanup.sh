#!/usr/bin/env bash
# Remove everything setup.sh and run.py created for the live trace test.
#
#   ODCL_LIVE_PROJECT=my-sandbox-project bash examples/live_trace/cleanup.sh
set -euo pipefail
P=${ODCL_LIVE_PROJECT:?Set ODCL_LIVE_PROJECT}
REGION=${ODCL_LIVE_REGION:-us-central1}
for d in odcl_demo_raw odcl_demo_features odcl_demo_frozen odcl_demo_assertions; do bq rm -r -f -d "$P:$d" || true; done
gcloud storage rm -r "gs://$P-odcl-live" --quiet || true
curl -s -X DELETE -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://dataform.googleapis.com/v1/projects/$P/locations/$REGION/repositories/odcl-live-test?force=true" >/dev/null || true
for role in roles/bigquery.jobUser; do
  gcloud projects remove-iam-policy-binding "$P" --condition=None --quiet --member "serviceAccount:odcl-dataform-test@$P.iam.gserviceaccount.com" --role $role >/dev/null || true
done
gcloud projects remove-iam-policy-binding "$P" --condition=None --quiet --member "serviceAccount:odcl-vertex-test@$P.iam.gserviceaccount.com" --role roles/aiplatform.user >/dev/null || true
for name in odcl-dataform-test odcl-vertex-test; do gcloud iam service-accounts delete "$name@$P.iam.gserviceaccount.com" --project "$P" --quiet || true; done
echo "Removed. Vertex AI pipeline runs stay in the run history; delete them in the console if you want them gone."
