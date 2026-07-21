#!/usr/bin/env bash
# Manage GCP Cloud Scheduler jobs for the daily poster — one job per channel in
# ops/scheduler/channels.conf. Each job HTTP-POSTs the trigger sidecar (through
# the Cloudflare tunnel), which runs run-daily.sh on the host where the stack
# lives. Cloud Scheduler is only the timer; it cannot run the posting logic.
#
# Gated like azure-deploy.sh: refuses unless GCP_PROD_SCHEDULER=enabled in .env.
# Usage: gcp-scheduler.sh <create|delete|list|run|logs|show>
# Set DRY=1 to print the gcloud commands instead of running them (no auth needed).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
# shellcheck source=ops/scheduler/lib.sh
source "$DIR/ops/scheduler/lib.sh"

CMD="${1:-}"
[ -n "$CMD" ] || { echo "usage: gcp-scheduler.sh <create|delete|list|run|logs|show>"; exit 2; }

# --- gate + required config (mirrors azure-deploy.sh) ------------------------
[ -f .env ] || { echo "ERROR: .env not found"; exit 1; }
FLAG="$(sched_envget GCP_PROD_SCHEDULER)"
case "${FLAG:-disabled}" in
  enabled|true|1) : ;;
  *) echo "ERROR: GCP_PROD_SCHEDULER is not 'enabled' in .env. Set it to proceed."; exit 1 ;;
esac

PROJECT="$(sched_envget GCP_PROJECT)"
REGION="$(sched_envget GCP_SCHEDULER_REGION)"
TZ_="$(sched_envget GCP_SCHEDULER_TZ)"; TZ_="${TZ_:-Etc/UTC}"
TRIGGER_URL="$(sched_envget SCHEDULER_TRIGGER_URL)"
TRIGGER_TOKEN="$(sched_envget SCHEDULER_TRIGGER_TOKEN)"
: "${PROJECT:?GCP_PROJECT is required in .env}"
: "${REGION:?GCP_SCHEDULER_REGION is required in .env (e.g. us-central1)}"

# create/run/delete need the trigger target + auth; list/logs don't.
case "$CMD" in
  create|run)
    : "${TRIGGER_URL:?SCHEDULER_TRIGGER_URL is required (public /run URL via the CF tunnel)}"
    : "${TRIGGER_TOKEN:?SCHEDULER_TRIGGER_TOKEN is required (shared secret the trigger checks)}" ;;
esac

if [ "${DRY:-}" != "1" ]; then
  command -v gcloud >/dev/null || { echo "ERROR: gcloud CLI not installed"; exit 1; }
fi

gc() {  # echo-or-exec gcloud
  if [ "${DRY:-}" = "1" ]; then printf '  + gcloud %s\n' "$*"; else gcloud "$@"; fi
}

job_name() { echo "postiz-daily-$1"; }

job_exists() {
  [ "${DRY:-}" = "1" ] && return 1
  gcloud scheduler jobs describe "$(job_name "$1")" \
    --project="$PROJECT" --location="$REGION" >/dev/null 2>&1
}

create_jobs() {
  while IFS=$'\t' read -r channel count delay tier cron; do
    local name body verb hdr; name="$(job_name "$channel")"
    body="$(printf '{"channel":"%s","count":"%s","delay":"%s","tier":"%s"}' "$channel" "$count" "$delay" "$tier")"
    # gcloud quirk: `create http` takes --headers, `update http` takes --update-headers.
    if job_exists "$channel"; then verb=update; hdr=--update-headers; else verb=create; hdr=--headers; fi
    echo "==> $verb $name  ('$cron' $TZ_)  count=$count delay=$delay tier=$tier"
    gc scheduler jobs "$verb" http "$name" \
      --project="$PROJECT" --location="$REGION" \
      --schedule="$cron" --time-zone="$TZ_" \
      --uri="$TRIGGER_URL" --http-method=POST \
      "$hdr=Content-Type=application/json,Authorization=Bearer $TRIGGER_TOKEN" \
      --message-body="$body" \
      --attempt-deadline=60s
  done < <(sched_rows)
}

delete_jobs() {
  while IFS=$'\t' read -r channel _count _delay _tier _cron; do
    echo "==> delete $(job_name "$channel")"
    gc scheduler jobs delete "$(job_name "$channel")" \
      --project="$PROJECT" --location="$REGION" --quiet || true
  done < <(sched_rows)
}

run_jobs() {
  while IFS=$'\t' read -r channel _count _delay _tier _cron; do
    echo "==> run now: $(job_name "$channel")"
    gc scheduler jobs run "$(job_name "$channel")" --project="$PROJECT" --location="$REGION"
  done < <(sched_rows)
}

# Print each job's cron + decoded request body (the count/delay/tier that GCP
# POSTs to the trigger live base64-encoded in the body). Read-only.
show_jobs() {
  while IFS=$'\t' read -r channel _c _d _t _cron; do
    local name; name="$(job_name "$channel")"
    echo "== $name =="
    gcloud scheduler jobs describe "$name" --project="$PROJECT" --location="$REGION" --format=json 2>/dev/null \
      | python3 -c "import sys,json,base64
try: d=json.load(sys.stdin)
except Exception: print('  (not found — run make scheduler-up)'); raise SystemExit
h=d.get('httpTarget',{})
print('  cron :',d.get('schedule'),d.get('timeZone'))
print('  state:',d.get('state'))
print('  uri  :',h.get('uri'))
b=h.get('body'); print('  body :',base64.b64decode(b).decode() if b else '(none)')" \
      || echo "  (not found — run make scheduler-up)"
  done < <(sched_rows)
}

case "$CMD" in
  create) create_jobs ;;
  delete) delete_jobs ;;
  run)    run_jobs ;;
  show)   show_jobs ;;
  list)   gc scheduler jobs list --project="$PROJECT" --location="$REGION" ;;
  logs)   gc logging read \
            "resource.type=cloud_scheduler_job AND resource.labels.job_id:postiz-daily-" \
            --project="$PROJECT" --limit=50 --freshness=1d ;;
  *) echo "unknown command: $CMD"; exit 2 ;;
esac
