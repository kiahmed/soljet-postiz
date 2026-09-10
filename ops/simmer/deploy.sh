#!/usr/bin/env bash
# Provision the Facades posting pipeline on GCP — one command per product,
# but the identity + events topic are SHARED across all Facades products
# (simmer, matrix, torque). Nothing here touches the robotics pipeline.
#
#   ops/simmer/deploy.sh <product> [--sa-only|--snap-only|--poster-only|--pubsub-only|--sub-local|all]
#
# Everything except --sa-only first checks the <product> tier is ENABLED
# (registered in _TIER_FILE_BY_ID and with a live channel id in .env) — you
# don't subscribe to the topic for a product that can't post.
# --sub-local makes a PULL subscription on the real topic so you can validate
# against live EdgeLane events before the Cloud Run poster exists.
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
# CONFIG: GCP_PROJECT / GCP_REGION / GCP_SA_EMAIL / FACADES_EVENTS_TOPIC /
# FACADES_RUNTIME_SA_ID / POSTIZ_API_KEY_SECRET resolve as
#   explicit env var > repo-root .env > gcloud config / built-in default.
# So a plain `.env` with GCP_PROJECT set is enough — no `gcloud config set`.
#
# PROVISIONING IDENTITY: market-agent-sa (the owner/deployer, $GCP_SA_EMAIL /
# GOOGLE_APPLICATION_CREDENTIALS). Pass --activate to `gcloud auth
# activate-service-account` with that key first; otherwise the active gcloud
# account is used. DRY=1 prints every command instead of running it.
set -uo pipefail

PRODUCT="${1:?usage: deploy.sh <product> [--sa-only|--snap-only|--poster-only|--pubsub-only|--sub-local|all]}"
MODE="${2:-all}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/../.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python3"; [ -x "$PY" ] || PY=python3

# Read one KEY from repo-root .env without sourcing it (values may hold #, quotes,
# spaces). Same helper shape as ops/scheduler/run-daily.sh::envget.
envget(){ [ -f .env ] || return 0; grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- \
          | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" -e 's/\r$//' -e 's/[[:space:]]*$//'; }

# Precedence for every setting: explicit env var > .env > gcloud config / default.
PROJECT="${GCP_PROJECT:-$(envget GCP_PROJECT)}"
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${GCP_REGION:-$(envget GCP_REGION)}"
REGION="${REGION:-${GCP_SCHEDULER_REGION:-$(envget GCP_SCHEDULER_REGION)}}"
REGION="${REGION:-us-central1}"
TOPIC="${FACADES_EVENTS_TOPIC:-$(envget FACADES_EVENTS_TOPIC)}"
TOPIC="${TOPIC:-facades.ticker-events}"

[ -n "$PROJECT" ] || { echo "!! no project — set GCP_PROJECT in .env (or the env, or 'gcloud config set project')" >&2; exit 1; }

# Shared runtime SA for ALL Facades products (not per-product).
RUNTIME_SA_ID="${FACADES_RUNTIME_SA_ID:-$(envget FACADES_RUNTIME_SA_ID)}"
RUNTIME_SA_ID="${RUNTIME_SA_ID:-facades-poster-sa}"
RUNTIME_SA="${RUNTIME_SA_ID}@${PROJECT}.iam.gserviceaccount.com"
# Deployer / owner — provisions everything below.
DEPLOYER_SA="${GCP_SA_EMAIL:-$(envget GCP_SA_EMAIL)}"
DEPLOYER_SA="${DEPLOYER_SA:-market-agent-sa@${PROJECT}.iam.gserviceaccount.com}"

SNAP_SVC="${PRODUCT}-snap"
POSTER_SVC="${PRODUCT}-poster"
SUB="${PRODUCT}-poster-sub"
# Secret Manager ids the poster mounts.
SECRET_POSTIZ="${POSTIZ_API_KEY_SECRET:-$(envget POSTIZ_API_KEY_SECRET)}"
SECRET_POSTIZ="${SECRET_POSTIZ:-postiz-api-key}"
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

# --- precondition: the product tier must be enabled --------------------
require_tier_ready(){
  echo "== Precondition: '$PRODUCT' tier enabled =="
  "$PY" - "$PRODUCT" <<'PY'
import sys
sys.path.insert(0, ".")
from bin._common import load_dotenv, integration_ids_for
from src.lib.config_loader import known_tiers, load_tier
tid = sys.argv[1]
load_dotenv()
if tid not in known_tiers():
    sys.exit(f"  x '{tid}' is not a registered tier (add it to _TIER_FILE_BY_ID). Known: {known_tiers()}")
try:
    t = load_tier(tid)
except Exception as e:
    sys.exit(f"  x load_tier('{tid}') failed: {e}")
iids = integration_ids_for(t)
if not iids:
    sys.exit(f"  x '{tid}' has no live channels — set its POSTIZ_INTEGRATION_ID_* in .env "
             f"(LINKEDIN_ENABLED too, for LinkedIn).")
print(f"  ok  {tid}: {len(iids)} channel(s) -> {iids}")
PY
  local rc=$?
  [ $rc -eq 0 ] || { echo "  refusing to provision the subscription for a disabled product."; exit 3; }
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
  local snap_url img
  snap_url="$(gcloud run services describe "$SNAP_SVC" --project="$PROJECT" --region="$REGION" --format='value(status.url)' 2>/dev/null)"
  [ -n "$snap_url" ] || { snap_url="https://${SNAP_SVC}-REPLACE-uc.a.run.app"; echo "   ! $SNAP_SVC not deployed — SIMMER_SNAP_URL will need patching"; }
  img="${REGION}-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy/${POSTER_SVC}:latest"
  # The poster Dockerfile COPYs bin/ src/ products/, so the build context must be
  # the repo root — `run deploy --source` can't target a non-root Dockerfile, so
  # build via cloudbuild.yaml then deploy the image.
  gc builds submit "$ROOT" --config="$ROOT/ops/simmer/poster/cloudbuild.yaml" \
    --substitutions="_IMAGE=${img}"

  # The tier.config resolves its channels from ${POSTIZ_INTEGRATION_ID_*_<P>} /
  # ${LINKEDIN_ENABLED} — the container has no .env, so pass them through.
  local P; P="$(echo "$PRODUCT" | tr '[:lower:]' '[:upper:]')"
  local chan_env=""
  for k in "POSTIZ_INTEGRATION_ID_LINKEDIN_${P}" "POSTIZ_INTEGRATION_ID_X_${P}" \
           "POSTIZ_CUSTOMER_ID_${P}" LINKEDIN_ENABLED; do
    v="$(envget "$k")"; [ -n "$v" ] && chan_env="${chan_env},${k}=${v}"
  done

  gc run deploy "$POSTER_SVC" --region="$REGION" \
    --image="$img" \
    --no-allow-unauthenticated \
    --service-account="$RUNTIME_SA" \
    --memory=512Mi --cpu=1 --timeout=120 \
    --set-env-vars="GCP_PROJECT=${PROJECT},SIMMER_PUBSUB_SUBSCRIPTION=${SUB},SIMMER_API_BASE=https://edge.facades.trade,SIMMER_SNAP_URL=${snap_url}/snap,POSTIZ_API_URL=https://dev.arboryx.ai,SIMMER_DEDUPE_COLLECTION=${PRODUCT}_poster_dedupe${chan_env}" \
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

# PULL subscription on the shared topic — for validating against real EdgeLane
# events with `make simmer-poster` before the Cloud Run poster exists. Same
# per-product filter. Idempotent.
deploy_sub_local(){
  local sub_local="${SUB}-local"
  echo "== Pub/Sub PULL subscription (local test): $sub_local =="
  ensure_topic
  gc pubsub subscriptions create "$sub_local" \
    --topic="$TOPIC" \
    --message-filter="attributes.product=\"${PRODUCT}\"" \
    --ack-deadline=60 --message-retention-duration=1h 2>/dev/null \
    || echo "   (exists)"
  # whoever runs `make simmer-poster` locally (GOOGLE_APPLICATION_CREDENTIALS
  # = the deployer SA) needs to pull it:
  gc pubsub subscriptions add-iam-policy-binding "$sub_local" \
    --member="serviceAccount:${DEPLOYER_SA}" --role="roles/pubsub.subscriber" 2>/dev/null || true
  echo
  echo "   drain it:  SIMMER_PUBSUB_SUBSCRIPTION=$sub_local make simmer-poster MODE=draft"
}

case "$MODE" in
  --sa-only)     ensure_topic; ensure_sa ;;
  --sub-local)   require_tier_ready; deploy_sub_local ;;
  --snap-only)   require_tier_ready; deploy_snap ;;
  --poster-only) require_tier_ready; deploy_poster ;;
  --pubsub-only) require_tier_ready; deploy_pubsub ;;
  all)           require_tier_ready; ensure_topic; ensure_sa; deploy_snap; deploy_poster; deploy_pubsub ;;
  *) echo "unknown mode $MODE"; exit 2 ;;
esac
echo "done."
