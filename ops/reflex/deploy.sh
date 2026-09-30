#!/usr/bin/env bash
# Provision Reflex's posting pipeline on GCP.
#
# Forked from ops/matrix/deploy.sh, minus the snap service: Reflex
# (facades-news-reactor) ships the finished image + caption in its own
# Supabase, so there is nothing to render here. See docs/reflex_integration.md.
#
#   ops/reflex/deploy.sh reflex [--sa-only|--poster-only|--pubsub-only|--sub-local|all]
#
# --pubsub-only / all first check the reflex tier is ENABLED (registered + a
# live channel id in .env) — don't subscribe for a product that can't post.
# --poster-only doesn't: a channel-less poster just logs "no-channels", so the
# image can be built and deployed before the accounts exist.
# --sub-local makes a PULL subscription on the real topic for local dry runs
# (`make reflex-poster DRY=1 SUB=...-local`) — no channels needed either.
#
# Reflex's (NOT created here — Reflex owns and publishes to it):
#   Pub/Sub topic   facades.reflex-signal-posts   (project marketresearch-agents)
#
# Shared with Simmer/Matrix (created once, idempotent):
#   Service account   facades-poster-sa@<project>   — the RUNTIME identity for
#                      reflex-poster and the Pub/Sub push auth. Roles:
#                        roles/pubsub.subscriber   (on our subscription)
#                        roles/run.invoker         (on reflex-poster)
#                        roles/secretmanager.secretAccessor
#                                                  (postiz-api-key, reflex-db-url)
#                        roles/datastore.user      (project — Firestore dedupe)
#
# Reflex's own:
#   Secret      reflex-db-url   news-reactor's Supabase owner/pooler URL, pushed
#                               from REFLEX_SUPABASE_DB_URL in .env by --sa-only
#                               (RLS is on — the anon key can't read the table)
#   Cloud Run   reflex-poster   ops/reflex/poster (Pub/Sub push subscriber)
#   Pub/Sub sub facades.reflex-signal-posts.postiz   push -> reflex-poster,
#               OIDC as facades-poster-sa, filter attributes.product="reflex"
#
# CONFIG: GCP_PROJECT / GCP_REGION / GCP_SA_EMAIL / REFLEX_POSTS_TOPIC /
# REFLEX_PUBSUB_SUBSCRIPTION / FACADES_RUNTIME_SA_ID / POSTIZ_API_KEY_SECRET
# resolve as explicit env var > repo-root .env > gcloud config / default.
#
# PROVISIONING IDENTITY: whoever gcloud is logged in as (market-agent-sa, the
# owner/deployer, as for Simmer/Matrix). --activate / ACTIVATE=1 logs in with
# $GOOGLE_APPLICATION_CREDENTIALS first. DRY=1 prints every command instead.
set -uo pipefail

PRODUCT="${1:?usage: deploy.sh reflex [--sa-only|--poster-only|--pubsub-only|--sub-local|all]}"
MODE="${2:-all}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/../.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python3"; [ -x "$PY" ] || PY=python3

# Read one KEY from repo-root .env without sourcing it (values may hold #, quotes,
# spaces). Same helper shape as ops/scheduler/run-daily.sh::envget.
envget(){ [ -f .env ] || return 0; grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- \
          | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" -e 's/\r$//' -e 's/[[:space:]]*$//'; }

PROJECT="${GCP_PROJECT:-$(envget GCP_PROJECT)}"
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${GCP_REGION:-$(envget GCP_REGION)}"
REGION="${REGION:-${GCP_SCHEDULER_REGION:-$(envget GCP_SCHEDULER_REGION)}}"
REGION="${REGION:-us-central1}"
TOPIC="${REFLEX_POSTS_TOPIC:-$(envget REFLEX_POSTS_TOPIC)}"
TOPIC="${TOPIC:-facades.reflex-signal-posts}"

[ -n "$PROJECT" ] || { echo "!! no project — set GCP_PROJECT in .env (or the env, or 'gcloud config set project')" >&2; exit 1; }

RUNTIME_SA_ID="${FACADES_RUNTIME_SA_ID:-$(envget FACADES_RUNTIME_SA_ID)}"
RUNTIME_SA_ID="${RUNTIME_SA_ID:-facades-poster-sa}"
RUNTIME_SA="${RUNTIME_SA_ID}@${PROJECT}.iam.gserviceaccount.com"
DEPLOYER_SA="${GCP_SA_EMAIL:-$(envget GCP_SA_EMAIL)}"
DEPLOYER_SA="${DEPLOYER_SA:-market-agent-sa@${PROJECT}.iam.gserviceaccount.com}"

POSTER_SVC="${PRODUCT}-poster"
SUB="${REFLEX_PUBSUB_SUBSCRIPTION:-$(envget REFLEX_PUBSUB_SUBSCRIPTION)}"
SUB="${SUB:-facades.reflex-signal-posts.postiz}"
SECRET_POSTIZ="${POSTIZ_API_KEY_SECRET:-$(envget POSTIZ_API_KEY_SECRET)}"
SECRET_POSTIZ="${SECRET_POSTIZ:-postiz-api-key}"
SECRET_DB="${PRODUCT}-db-url"

run(){ if [ "${DRY:-}" = 1 ]; then printf '  + %s\n' "$*"; else "$@"; fi; }
gc(){ run gcloud "$@" --project="$PROJECT"; }

echo "product=$PRODUCT  project=$PROJECT  region=$REGION"
echo "deployer=$DEPLOYER_SA  runtime=$RUNTIME_SA  topic=$TOPIC  sub=$SUB"

if [ "${3:-}" = "--activate" ] || [ "${ACTIVATE:-}" = 1 ]; then
  run gcloud auth activate-service-account "$DEPLOYER_SA" \
    --key-file="${GOOGLE_APPLICATION_CREDENTIALS:?set GOOGLE_APPLICATION_CREDENTIALS}"
fi

# --- Reflex's topic: must already exist (Reflex's ops/reflex/provision.sh) --
require_topic(){
  echo "== Pub/Sub topic: $TOPIC (Reflex-owned) =="
  [ "${DRY:-}" = 1 ] && { echo "  + (would check the topic exists)"; return 0; }
  gcloud pubsub topics describe "$TOPIC" --project="$PROJECT" --format='value(name)' >/dev/null 2>&1 \
    || { echo "  x topic $TOPIC not found — Reflex creates it (news-reactor ops/reflex/provision.sh)"; exit 4; }
  echo "   ok"
}

# --- DB secret: pushed from .env; a new version only when the value changed --
ensure_db_secret(){
  echo "== Secret: $SECRET_DB (news-reactor Supabase owner URL) =="
  local val cur
  val="$(envget REFLEX_SUPABASE_DB_URL)"
  if [ -z "$val" ]; then
    echo "   ! REFLEX_SUPABASE_DB_URL unset in .env — copy SUPABASE_DB_URL from facades-news-reactor/.env"
    return 1
  fi
  if [ "${DRY:-}" = 1 ]; then
    echo "  + gcloud secrets create|versions add $SECRET_DB --data-file=- (value from .env, not printed)"
    return 0
  fi
  if ! gcloud secrets describe "$SECRET_DB" --project="$PROJECT" --format='value(name)' >/dev/null 2>&1; then
    printf %s "$val" | gcloud secrets create "$SECRET_DB" --project="$PROJECT" \
      --replication-policy=automatic --data-file=- && echo "   created"
  else
    cur="$(gcloud secrets versions access latest --secret="$SECRET_DB" --project="$PROJECT" 2>/dev/null)"
    if [ "$cur" = "$val" ]; then echo "   (up to date)"
    else printf %s "$val" | gcloud secrets versions add "$SECRET_DB" --project="$PROJECT" \
           --data-file=- && echo "   new version added (redeploy the poster to pick it up)"; fi
  fi
}

ensure_sa(){
  echo "== Service account: $RUNTIME_SA =="
  gc iam service-accounts create "$RUNTIME_SA_ID" \
    --display-name="Facades posting - Pub/Sub subscriber + poster runtime" \
    2>/dev/null || echo "   (exists)"
  gc iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
    --member="serviceAccount:${DEPLOYER_SA}" --role="roles/iam.serviceAccountUser"
  gc projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${RUNTIME_SA}" --role="roles/datastore.user" \
    --condition=None
  for s in "$SECRET_POSTIZ" "$SECRET_DB"; do
    gc secrets add-iam-policy-binding "$s" \
      --member="serviceAccount:${RUNTIME_SA}" \
      --role="roles/secretmanager.secretAccessor" 2>/dev/null \
      || echo "   ! secret '$s' not found yet — create it, then re-run --sa-only"
  done
}

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
t = load_tier(tid)
iids = integration_ids_for(t)
if not iids:
    sys.exit(f"  x '{tid}' has no live channels — set POSTIZ_INTEGRATION_ID_*_REFLEX in .env "
             f"(LINKEDIN_ENABLED too, for LinkedIn).")
print(f"  ok  {tid}: {len(iids)} channel(s) -> {iids}")
PY
  local rc=$?
  [ $rc -eq 0 ] || { echo "  refusing to provision the subscription for a disabled product."; exit 3; }
}

deploy_poster(){
  echo "== Cloud Run: $POSTER_SVC =="
  local img
  img="${REGION}-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy/${POSTER_SVC}:latest"
  gc builds submit "$ROOT" --config="$ROOT/ops/reflex/poster/cloudbuild.yaml" \
    --substitutions="_IMAGE=${img}"

  # The tier.config resolves its channels from ${POSTIZ_INTEGRATION_ID_*_REFLEX}
  # / ${LINKEDIN_ENABLED} — the container has no .env, so pass them through.
  local P; P="$(echo "$PRODUCT" | tr '[:lower:]' '[:upper:]')"
  local chan_env=""
  for k in "POSTIZ_INTEGRATION_ID_LINKEDIN_${P}" "POSTIZ_INTEGRATION_ID_X_${P}" \
           "POSTIZ_CUSTOMER_ID_${P}" LINKEDIN_ENABLED; do
    v="$(envget "$k")"; [ -n "$v" ] && chan_env="${chan_env},${k}=${v}"
  done
  [ -n "$(envget "POSTIZ_INTEGRATION_ID_X_${P}")$(envget "POSTIZ_INTEGRATION_ID_LINKEDIN_${P}")" ] \
    || echo "   ! no channel ids in .env yet — the poster will log 'no-channels'; redeploy after connecting accounts"

  gc run deploy "$POSTER_SVC" --region="$REGION" \
    --image="$img" \
    --no-allow-unauthenticated \
    --service-account="$RUNTIME_SA" \
    --memory=512Mi --cpu=1 --timeout=120 \
    --set-env-vars="GCP_PROJECT=${PROJECT},REFLEX_PUBSUB_SUBSCRIPTION=${SUB},POSTIZ_API_URL=https://dev.arboryx.ai,REFLEX_DEDUPE_COLLECTION=${PRODUCT}_poster_dedupe${chan_env}" \
    --set-secrets="POSTIZ_API_KEY=${SECRET_POSTIZ}:latest,REFLEX_SUPABASE_DB_URL=${SECRET_DB}:latest"
  # Pub/Sub push (auth as RUNTIME_SA) invokes the poster:
  gc run services add-iam-policy-binding "$POSTER_SVC" --region="$REGION" \
    --member="serviceAccount:${RUNTIME_SA}" --role="roles/run.invoker"
}

# Dead-letter backstop — same reasoning as ops/simmer/deploy.sh::ensure_dlq:
# the poster always acks (204), so this only catches a crash where Cloud Run
# never answers at all (GCP's floor is 5 attempts).
ensure_dlq(){
  local dlq_topic="${SUB}-dlq" dlq_sub="${SUB}-dlq-sub" proj_num pubsub_sa
  echo "== Pub/Sub dead-letter: $dlq_topic =="
  gc pubsub topics create "$dlq_topic" 2>/dev/null || echo "   (exists)"
  gc pubsub subscriptions create "$dlq_sub" --topic="$dlq_topic" \
    --ack-deadline=60 --message-retention-duration=7d 2>/dev/null || echo "   (exists)"
  proj_num="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
  pubsub_sa="service-${proj_num}@gcp-sa-pubsub.iam.gserviceaccount.com"
  gc pubsub topics add-iam-policy-binding "$dlq_topic" \
    --member="serviceAccount:${pubsub_sa}" --role="roles/pubsub.publisher"
  gc pubsub subscriptions add-iam-policy-binding "$SUB" \
    --member="serviceAccount:${pubsub_sa}" --role="roles/pubsub.subscriber"
}

deploy_pubsub(){
  echo "== Pub/Sub subscription: $SUB =="
  require_topic
  local push_url
  push_url="$(gcloud run services describe "$POSTER_SVC" --project="$PROJECT" --region="$REGION" --format='value(status.url)' 2>/dev/null)"
  if [ -z "$push_url" ]; then
    [ "${DRY:-}" = 1 ] && push_url="https://${POSTER_SVC}-REPLACE.a.run.app" \
      || { echo "  ! $POSTER_SVC not deployed yet — run --poster-only first"; return 1; }
  fi
  # ack-deadline 120: a push holds the row lock while it uploads + posts.
  gc pubsub subscriptions create "$SUB" \
    --topic="$TOPIC" \
    --message-filter="attributes.product=\"${PRODUCT}\"" \
    --push-endpoint="${push_url}/" \
    --push-auth-service-account="$RUNTIME_SA" \
    --ack-deadline=120 --min-retry-delay=10s --max-retry-delay=300s 2>/dev/null \
  || gc pubsub subscriptions update "$SUB" \
    --push-endpoint="${push_url}/" \
    --push-auth-service-account="$RUNTIME_SA"
  gc pubsub subscriptions add-iam-policy-binding "$SUB" \
    --member="serviceAccount:${RUNTIME_SA}" --role="roles/pubsub.subscriber"
  ensure_dlq
  gc pubsub subscriptions update "$SUB" \
    --dead-letter-topic="${SUB}-dlq" --max-delivery-attempts=5
}

# PULL subscription on Reflex's topic for local validation — `make
# reflex-poster DRY=1 SUB=<this>`. Same filter. Idempotent.
deploy_sub_local(){
  local sub_local="${SUB}-local"
  echo "== Pub/Sub PULL subscription (local test): $sub_local =="
  require_topic
  gc pubsub subscriptions create "$sub_local" \
    --topic="$TOPIC" \
    --message-filter="attributes.product=\"${PRODUCT}\"" \
    --ack-deadline=120 --message-retention-duration=1d 2>/dev/null \
    || echo "   (exists)"
  gc pubsub subscriptions add-iam-policy-binding "$sub_local" \
    --member="serviceAccount:${DEPLOYER_SA}" --role="roles/pubsub.subscriber" 2>/dev/null || true
  echo
  echo "   drain it:  make reflex-poster DRY=1 SUB=$sub_local"
}

case "$MODE" in
  --sa-only)     ensure_db_secret; ensure_sa ;;
  --sub-local)   deploy_sub_local ;;
  --poster-only) deploy_poster ;;
  --pubsub-only) require_tier_ready; deploy_pubsub ;;
  all)           require_tier_ready; require_topic; ensure_db_secret; ensure_sa; deploy_poster; deploy_pubsub ;;
  *) echo "unknown mode $MODE"; exit 2 ;;
esac
echo "done."
