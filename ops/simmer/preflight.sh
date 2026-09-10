#!/usr/bin/env bash
# Preflight for a Facades product's posting pipeline. Checks every dependency
# ops/simmer/deploy.sh needs (and the Cloud Run poster needs at runtime) and
# prints each one — OK in green, a problem in red, an advisory in yellow.
#
#   ops/simmer/preflight.sh <product>        # e.g. simmer | matrix | torque
#
# Exit 0 only if nothing is red. Reuses deploy.sh's config resolution:
#   explicit env var > repo-root .env > gcloud config / default.
set -uo pipefail

PRODUCT="${1:?usage: preflight.sh <product>}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/../.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python3"; [ -x "$PY" ] || PY=python3

envget(){ [ -f .env ] || return 0; grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- \
          | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" -e 's/\r$//' -e 's/[[:space:]]*$//'; }

PROJECT="${GCP_PROJECT:-$(envget GCP_PROJECT)}"; PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${GCP_REGION:-$(envget GCP_REGION)}"; REGION="${REGION:-${GCP_SCHEDULER_REGION:-$(envget GCP_SCHEDULER_REGION)}}"; REGION="${REGION:-us-central1}"
TOPIC="${FACADES_EVENTS_TOPIC:-$(envget FACADES_EVENTS_TOPIC)}"; TOPIC="${TOPIC:-facades.ticker-events}"
RUNTIME_SA_ID="${FACADES_RUNTIME_SA_ID:-$(envget FACADES_RUNTIME_SA_ID)}"; RUNTIME_SA_ID="${RUNTIME_SA_ID:-facades-poster-sa}"
DEPLOYER_SA="${GCP_SA_EMAIL:-$(envget GCP_SA_EMAIL)}"; DEPLOYER_SA="${DEPLOYER_SA:-market-agent-sa@${PROJECT}.iam.gserviceaccount.com}"
SECRET_POSTIZ="${POSTIZ_API_KEY_SECRET:-$(envget POSTIZ_API_KEY_SECRET)}"; SECRET_POSTIZ="${SECRET_POSTIZ:-postiz-api-key}"

RUNTIME_SA="${RUNTIME_SA_ID}@${PROJECT}.iam.gserviceaccount.com"
SECRET_TOKEN="${PRODUCT}-api-token"
SNAP_SVC="${PRODUCT}-snap"
POSTER_SVC="${PRODUCT}-poster"
SUB="${PRODUCT}-poster-sub"

# ── output helpers ─────────────────────────────────────────────────────────
if [ -t 1 ]; then G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; B=$'\e[1m'; Z=$'\e[0m'; else G=; R=; Y=; B=; Z=; fi
fails=0; warns=0
ok()   { printf "  %s%-42s%s %sOK%s   %s\n"   "$Z" "$1" "$Z" "$G" "$Z" "${2:-}"; }
bad()  { printf "  %s%-42s%s %sFAIL%s %s\n"   "$Z" "$1" "$Z" "$R" "$Z" "${2:-}"; fails=$((fails+1)); }
warn() { printf "  %s%-42s%s %sWARN%s %s\n"   "$Z" "$1" "$Z" "$Y" "$Z" "${2:-}"; warns=$((warns+1)); }
section() { printf "\n%s%s%s\n" "$B" "$1" "$Z"; }

gcq(){ gcloud "$@" --project="$PROJECT" 2>/dev/null; }
# does MEMBER hold ROLE at the project level?
proj_has_role(){ gcloud projects get-iam-policy "$PROJECT" --flatten='bindings[].members' \
  --filter="bindings.members:$1 AND bindings.role:$2" --format='value(bindings.role)' 2>/dev/null | grep -q .; }

echo "${B}Facades preflight — product '${PRODUCT}'${Z}"
echo "  project=${PROJECT:-<unresolved>}  region=${REGION}  topic=${TOPIC}"
echo "  deployer=${DEPLOYER_SA}  runtime=${RUNTIME_SA}"

# ── 1. auth / project ─────────────────────────────────────────────────────
section "auth"
ACTIVE="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null)"
[ -n "$ACTIVE" ] && ok "gcloud active account" "$ACTIVE" || bad "gcloud active account" "run: gcloud auth activate-service-account / login"
[ -n "$PROJECT" ] && ok "project resolved" "$PROJECT" || bad "project resolved" "set GCP_PROJECT in .env"
[ -n "$PROJECT" ] || { echo; echo "${R}stopping — no project${Z}"; exit 1; }

# ── 2. deployer (market-agent-sa) IAM ────────────────────────────────────
section "deployer IAM ($DEPLOYER_SA)"
for role in roles/run.admin roles/cloudbuild.builds.editor roles/artifactregistry.admin \
            roles/iam.serviceAccountAdmin roles/secretmanager.admin roles/pubsub.admin; do
  if proj_has_role "serviceAccount:$DEPLOYER_SA" "$role"; then ok "$role"; else bad "$role" "grant: gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:$DEPLOYER_SA --role=$role"; fi
done

# ── 3. runtime SA (facades-poster-sa) ────────────────────────────────────
section "runtime SA ($RUNTIME_SA)"
if gcq iam service-accounts describe "$RUNTIME_SA" --format='value(email)' >/dev/null; then
  ok "service account exists"
  if proj_has_role "serviceAccount:$RUNTIME_SA" roles/datastore.user; then ok "roles/datastore.user (Firestore dedupe)"; else bad "roles/datastore.user" "make ${PRODUCT}-deploy PART=--sa-only"; fi
  if gcloud iam service-accounts get-iam-policy "$RUNTIME_SA" --project="$PROJECT" \
       --flatten='bindings[].members' --filter="bindings.members:serviceAccount:$DEPLOYER_SA AND bindings.role:roles/iam.serviceAccountUser" \
       --format='value(bindings.role)' 2>/dev/null | grep -q .; then
    ok "deployer can actAs it (serviceAccountUser)"
  else bad "serviceAccountUser for deployer" "make ${PRODUCT}-deploy PART=--sa-only"; fi
else
  bad "service account exists" "make ${PRODUCT}-deploy PART=--sa-only  (creates it)"
fi

# ── 4. Secret Manager ────────────────────────────────────────────────────
section "secrets"
for pair in "$SECRET_POSTIZ:shared Postiz API key" "$SECRET_TOKEN:${PRODUCT} read-only API token"; do
  s="${pair%%:*}"; desc="${pair##*:}"
  if gcq secrets describe "$s" --format='value(name)' >/dev/null; then
    ok "secret $s" "$desc"
    if gcloud secrets get-iam-policy "$s" --project="$PROJECT" --flatten='bindings[].members' \
         --filter="bindings.members:serviceAccount:$RUNTIME_SA AND bindings.role:roles/secretmanager.secretAccessor" \
         --format='value(bindings.role)' 2>/dev/null | grep -q .; then
      ok "  └ accessor for runtime SA"
    else bad "  └ accessor for runtime SA" "make ${PRODUCT}-deploy PART=--sa-only"; fi
  else
    bad "secret $s" "create it:  printf %s \"\$VALUE\" | gcloud secrets create $s --data-file=- --project=$PROJECT"
  fi
done

# ── 5. Pub/Sub ──────────────────────────────────────────────────────────
section "pub/sub"
if gcq pubsub topics describe "$TOPIC" --format='value(name)' >/dev/null; then ok "topic $TOPIC"; else bad "topic $TOPIC" "make ${PRODUCT}-deploy PART=--sa-only  (or EdgeLane's provisioner)"; fi
if gcq pubsub subscriptions describe "$SUB" --format='value(name)' >/dev/null; then
  filt="$(gcq pubsub subscriptions describe "$SUB" --format='value(filter)')"
  [ "$filt" = "attributes.product = \"${PRODUCT}\"" ] && ok "subscription $SUB" "push, filtered" \
    || warn "subscription $SUB" "filter is '${filt:-<none>}', expected attributes.product=\"${PRODUCT}\""
else
  warn "subscription $SUB" "not created yet — make ${PRODUCT}-deploy PART=--pubsub-only (needs the poster service first)"
fi
gcq pubsub subscriptions describe "${SUB}-local" --format='value(name)' >/dev/null \
  && ok "subscription ${SUB}-local" "local pull test sub" || true

# ── 6. Cloud Run services ──────────────────────────────────────────────
section "cloud run"
for svc in "$SNAP_SVC" "$POSTER_SVC"; do
  url="$(gcloud run services describe "$svc" --project="$PROJECT" --region="$REGION" --format='value(status.url)' 2>/dev/null)"
  if [ -n "$url" ]; then
    ok "service $svc" "$url"
    if gcloud run services get-iam-policy "$svc" --project="$PROJECT" --region="$REGION" \
         --flatten='bindings[].members' --filter="bindings.members:serviceAccount:$RUNTIME_SA AND bindings.role:roles/run.invoker" \
         --format='value(bindings.role)' 2>/dev/null | grep -q .; then
      ok "  └ run.invoker for runtime SA"
    else bad "  └ run.invoker for runtime SA" "make ${PRODUCT}-deploy PART=--${svc##*-}-only"; fi
  else
    warn "service $svc" "not deployed — make ${PRODUCT}-deploy PART=--${svc##*-}-only"
  fi
done

# ── 7. tier config + .env ─────────────────────────────────────────────
section "tier / .env"
"$PY" "$DIR/preflight_tier.py" "$PRODUCT" || fails=$((fails+1))

# ── verdict ──────────────────────────────────────────────────────────
echo
if [ "$fails" -gt 0 ]; then
  echo "${R}${B}preflight FAILED${Z} — $fails blocking, $warns advisory"
  exit 1
fi
[ "$warns" -gt 0 ] && echo "${Y}preflight OK with $warns advisory${Z}" || echo "${G}${B}preflight OK${Z}"
exit 0
