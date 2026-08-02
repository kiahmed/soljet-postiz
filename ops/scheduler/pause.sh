#!/usr/bin/env bash
# Pause / resume a channel's daily posting — the ONE switch that covers all
# three consumers of channels.conf (local cron, GCP jobs, boot catch-up).
#
#   pause.sh pause  x[,linkedin]     comment the rows out, resync scheduler
#   pause.sh resume x[,linkedin]     uncomment, resync
#   pause.sh status                  show each row's state
#
# A paused row keeps its config as `#PAUSED channel|...`, so resume restores the
# exact schedule. After toggling we run scheduler-ctl.sh up so the generated
# crontab, the GCP jobs, and the containers (whose catch-up snapshots the file
# at boot) all agree with the file — editing channels.conf alone changes a file
# nobody re-reads.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$HERE/channels.conf"
ACTION="${1:-status}"
CHANNELS="${2:-}"

status() {
  echo "channel schedule state:"
  grep -E "^#PAUSED |^[a-z]" "$CONF" | while read -r line; do
    case "$line" in
      "#PAUSED "*) echo "  PAUSED : ${line#\#PAUSED }" ;;
      *)           echo "  active : $line" ;;
    esac
  done
}

[ "$ACTION" = "status" ] && { status; exit 0; }
[ -n "$CHANNELS" ] || { echo "usage: pause.sh pause|resume <channel>[,<channel>]"; exit 2; }

changed=0
for ch in ${CHANNELS//,/ }; do
  ch="$(echo "$ch" | tr 'A-Z' 'a-z' | tr -d ' ')"
  case "$ACTION" in
    pause)
      if grep -qE "^$ch[[:space:]]*\|" "$CONF"; then
        sed -i -E "s/^($ch[[:space:]]*\|)/#PAUSED \1/" "$CONF"
        echo "[pause] $ch paused"; changed=1
      elif grep -qE "^#PAUSED $ch[[:space:]]*\|" "$CONF"; then
        echo "[pause] $ch already paused"
      else
        echo "[pause] $ch: no such channel row in channels.conf"; exit 2
      fi ;;
    resume)
      if grep -qE "^#PAUSED $ch[[:space:]]*\|" "$CONF"; then
        sed -i -E "s/^#PAUSED ($ch[[:space:]]*\|)/\1/" "$CONF"
        echo "[resume] $ch resumed"; changed=1
      elif grep -qE "^$ch[[:space:]]*\|" "$CONF"; then
        echo "[resume] $ch already active"
      else
        echo "[resume] $ch: no such channel row in channels.conf"; exit 2
      fi ;;
    *) echo "usage: pause.sh pause|resume|status"; exit 2 ;;
  esac
done

# Mirror the state onto the GCP jobs themselves: create_jobs only upserts rows
# still IN the conf, so a paused channel's cloud job would keep firing (and
# failing at the trigger whitelist). gcloud pause/resume matches our semantics.
gcp_mirror() {
  . "$HERE/lib.sh" 2>/dev/null || true
  [ "$(sched_envget GCP_PROD_SCHEDULER)" = "enabled" ] || return 0
  local proj region
  proj="$(sched_envget GCP_PROJECT)"; region="$(sched_envget GCP_SCHEDULER_REGION)"
  for ch in ${CHANNELS//,/ }; do
    ch="$(echo "$ch" | tr 'A-Z' 'a-z' | tr -d ' ')"
    if gcloud scheduler jobs "$1" "postiz-daily-$ch"          ${proj:+--project="$proj"} ${region:+--location="$region"} --quiet 2>/dev/null; then
      echo "[gcp] job postiz-daily-$ch ${1}d"
    else
      echo "[gcp] WARN: could not $1 postiz-daily-$ch (gcloud auth? job absent?) — trigger whitelist still blocks paused channels"
    fi
  done
}

status
if [ "${PAUSE_NO_SYNC:-0}" = "1" ]; then
  echo "[sync] PAUSE_NO_SYNC=1 — skipping gcloud + resync (test mode)"
elif [ "$changed" = "1" ]; then
  case "$ACTION" in
    pause)  gcp_mirror pause ;;
    resume) gcp_mirror resume ;;
  esac
  echo "[sync] resyncing scheduler (crontab + GCP jobs + containers)..."
  "$HERE/scheduler-ctl.sh" up
else
  echo "[sync] nothing changed — no resync needed"
fi
