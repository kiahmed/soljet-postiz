#!/usr/bin/env bash
# Provision the Facades posting pipeline on GCP — one command per product,
# but the identity + events topic are SHARED across all Facades products
# (simmer, matrix, torque). Nothing here touches the robotics pipeline.
#
#   ops/simmer/deploy.sh <product> [--sa-only|--snap-only|--poster-only|--pubsub-only|all]
#
# Shared, created once (idempotent):
#   Pub/Sub topic        facades.ticker-events
#   Service account      facades-poster-sa@<project>   — the RUNTIME identity for
#                        every Facades *-poster / *-snap service and the Pub/Sub
#                        push auth. Roles (resource-scoped, robotics-style):
#                          roles/pubsub.subscriber          (per subscription)
#                          roles/run.invoker                (per *-snap, *-poster)
#                          roles/secretmanager.secretAccessor (postiz-api-key,
#                                                              <product>-api-token)
#                          roles/datastore.user             (project — Firestore
#                                                            IAM has no collection scope)
#
# Per product (prefixed <product>):
#   Cloud Run   <product>-snap      ops/simmer/snap   (headless-Chromium crop)
#   Cloud Run   <product>-poster    ops/simmer/poster (Pub/Sub push subscriber)
#   Pub/Sub sub <product>-poster-sub   filter attributes.product="<product>",
#               push -> <product>-poster, OIDC as facades-poster-sa
#
# PROVISIONING IDENTITY: market-agent-sa (the owner/deployer, $GCP_SA_EMAIL /
# GOOGLE_APPLICATION_CREDENTIALS). Pass --activate to `gcloud auth
# activate-service-account` with that key first; otherwise the active gcloud
# account is used. DRY=1 prints every command instead of running it.
set -uo pipefail

PRODUCT="${1:?usage: deploy.sh <product> [--sa-only|--snap-only|--poster-only|--pubsub-only|all]}"
MODE="${2:-all}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/../.." && pwd)"

PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${GCP_REGION:-${GCP_SCHEDULER_REGION:-us-central1}}"
TOPIC="${FACADES_EVENTS_TOPIC:-facades.ticker-events}"

# Shared runtime SA for ALL Facades products (not per-product).
RUNTIME_SA_ID="${FACADES_RUNTIME_SA_ID:-facades-poster-sa}"
RUNTIME_SA="${RUNTIME_SA_ID}@${PROJECT}.iam.gserviceaccount.com"
# Deployer / owner — provisions everything below.
DEPLOYER_SA="${GCP_SA_EMAIL:-market-agent-sa@${PROJECT}.iam.gserviceaccount.com}"

SNAP_SVC="${PRODUCT}-snap"
POSTER_SVC="${PRODUCT}-poster"
SUB="${PRODUCT}-poster-sub"
# Secret Manager ids the poster mounts.
SECRET_POSTIZ="${POSTIZ_API_KEY_SECRET:-postiz-api-key}"
SECRET_TOKEN="${PRODUCT}-api-token"

run(){ if [ "${DRY:-}" = 1 ]; then printf '  + %s\n' "$*"; else "$@"; fi; }
gc(){ run gcloud "$@" --project="$PROJECT"; }

echo "product=$PRODUCT  project=$PROJECT  region=$REGION"
echo "deployer=$DEPLOYER_SA  runtime=$RUNTIME_SA  topic=$TOPIC"

if [ "${3:-}" = "--activate" ] || [ "${ACTIVATE:-}" = 1 ]; then
  run gcloud auth activate-service-account "$DEPLOYER_SA" \
    --key-file="${GOOGLE_APPLICATION_CREDENTIALS:?set GOOGLE_APPLICATION_CREDENTIALS}"
fi

# --- shared: topic + runtime SA + its role bindings -----------------------
ensure_topic(){
  echo "== Pub/Sub topic: $TOPIC =="
  gc pubsub topics create "$TOPIC" 2>/dev/null || echo "   (exists)"
}

ensure_sa(){
  echo "== Service account: $RUNTIME_SA =="
  gc iam service-accounts create "$RUNTIME_SA_ID" \
    --display-name="Facades posting - Pub/Sub subscriber + poster runtime" \
    2>/dev/null || echo "   (exists)"

  # Let the deployer mint tokens / set this SA on services + push subs.
  gc iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
    --member="serviceAccount:${DEPLOYER_SA}" --role="roles/iam.serviceAccountUser"

  # Firestore dedupe collection (project-scoped — no collection-level IAM).
  gc projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${RUNTIME_SA}" --role="roles/datastore.user" \
    --condition=None

  # Secrets: the shared Postiz key + this product's API token.
  for s in "$SECRET_POSTIZ" "$SECRET_TOKEN"; do
    gc secrets add-iam-policy-binding "$s" \
      --member="serviceAccount:${RUNTIME_SA}" \
      --role="roles/secretmanager.secretAccessor" 2>/dev/null \
      || echo "   ! secret '$s' not found yet — create it, then re-run --sa-only"
  done
}

# --- per product --------------------------------------------------------
deploy_snap(){
  echo "== Cloud Run: $SNAP_SVC =="
  gc run deploy "$SNAP_SVC" --region="$REGION" \
    --source="$DIR/snap" \
    --no-allow-unauthenticated \
    --service-account="$RUNTIME_SA" \
    --memory=1Gi --cpu=1 --concurrency=1 --timeout=90 \
    --set-env-vars="SIMMER_SITE=https://${PRODUCT}.facades.trade,SNAP_SELECTOR=[data-snap=\"card\"]"
  # poster (same SA) calls snap:
  gc run services add-iam-policy-binding "$SNAP_SVC" --region="$REGION" \
    --member="serviceAccount:${RUNTIME_SA}" --role="roles/run.invoker"
}

deploy_poster(){
  echo "== Cloud Run: $POSTER_SVC =="
  local snap_url
  snap_url="$(gcloud run services describe "$SNAP_SVC" --project="$PROJECT" --region="$REGION" --format='value(status.url)' 2>/dev/null)"
  [ -n "$snap_url" ] || { snap_url="https://${SNAP_SVC}-REPLACE-uc.a.run.app"; echo "   ! $SNAP_SVC not deployed — SIMMER_SNAP_URL will need patching"; }
  gc run deploy "$POSTER_SVC" --region="$REGION" \
    --source="$ROOT" --dockerfile="ops/simmer/poster/Dockerfile" \
    --no-allow-unauthenticated \
    --service-account="$RUNTIME_SA" \
    --memory=512Mi --cpu=1 --timeout=120 \
    --set-env-vars="GCP_PROJECT=${PROJECT},SIMMER_PUBSUB_SUBSCRIPTION=${SUB},SIMMER_API_BASE=https://edge.facades.trade,SIMMER_SNAP_URL=${snap_url}/snap,POSTIZ_API_URL=https://dev.arboryx.ai,SIMMER_DEDUPE_COLLECTION=${PRODUCT}_poster_dedupe" \
    --set-secrets="POSTIZ_API_KEY=${SECRET_POSTIZ}:latest,SIMMER_API_TOKEN=${SECRET_TOKEN}:latest"
  # Pub/Sub push (auth as RUNTIME_SA) invokes the poster:
  gc run services add-iam-policy-binding "$POSTER_SVC" --region="$REGION" \
    --member="serviceAccount:${RUNTIME_SA}" --role="roles/run.invoker"
}

deploy_pubsub(){
  echo "== Pub/Sub subscription: $SUB =="
  ensure_topic
  local push_url
  push_url="$(gcloud run services describe "$POSTER_SVC" --project="$PROJECT" --region="$REGION" --format='value(status.url)' 2>/dev/null)"
  [ -n "$push_url" ] || { echo "  ! $POSTER_SVC not deployed yet — run --poster-only first"; return 1; }
  gc pubsub subscriptions create "$SUB" \
    --topic="$TOPIC" \
    --message-filter="attributes.product=\"${PRODUCT}\"" \
    --push-endpoint="${push_url}/" \
    --push-auth-service-account="$RUNTIME_SA" \
    --ack-deadline=60 --min-retry-delay=10s --max-retry-delay=300s 2>/dev/null \
  || gc pubsub subscriptions update "$SUB" \
    --push-endpoint="${push_url}/" \
    --push-auth-service-account="$RUNTIME_SA"
  # subscriber role, scoped to this subscription:
  gc pubsub subscriptions add-iam-policy-binding "$SUB" \
    --member="serviceAccount:${RUNTIME_SA}" --role="roles/pubsub.subscriber"
}

case "$MODE" in
  --sa-only)     ensure_topic; ensure_sa ;;
  --snap-only)   deploy_snap ;;
  --poster-only) deploy_poster ;;
  --pubsub-only) deploy_pubsub ;;
  all)           ensure_topic; ensure_sa; deploy_snap; deploy_poster; deploy_pubsub ;;
  *) echo "unknown mode $MODE"; exit 2 ;;
esac
echo "done."
