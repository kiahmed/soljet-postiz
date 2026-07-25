#!/usr/bin/env bash
# Catch up on missed daily fires after a host outage.
#
# The stack runs on a laptop/WSL host: when it sleeps, cron fires (local mode)
# or GCP trigger calls (gcp mode) land on a dead box and the day silently loses
# posts. This runs ONCE at scheduler/trigger container boot: for each channel in
# channels.conf it counts how many fires should already have happened today
# (cron times in the past, container TZ) vs how many posts that channel actually
# PUBLISHED today (Postiz DB — so manual runs count toward the quota), and fires
# run-daily.sh once with the shortfall.
#
# Safe by construction:
#   - shortfall <= missed fires, so it never exceeds the day's configured budget
#   - daily.py's posted_log/pending dedup still applies underneath
#   - if the DB isn't reachable we do NOTHING (never post blind)
#   - CATCHUP_DISABLED=1 turns it off; CATCHUP_DRY=1 prints instead of running
#
# Test hooks: CATCHUP_NOW="HH:MM" fakes the current time; CATCHUP_COUNTS
# "channel=n,channel=n" fakes today's published counts (skips the DB).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib.sh"

log() { echo "[catch-up] $*"; }

[ "${CATCHUP_DISABLED:-0}" = "1" ] && { log "disabled — skipping"; exit 0; }

# In GCP mode both containers boot; only the ACTIVE backend may catch up, or a
# wake-up would double-post. Caller passes its mode: local (supercronic) | gcp.
MODE="${1:-local}"
GCP_FLAG="$(sched_envget GCP_PROD_SCHEDULER)"
if [ "$GCP_FLAG" = "enabled" ] && [ "$MODE" != "gcp" ]; then
  log "GCP mode active — local container defers catch-up"; exit 0
fi
if [ "$GCP_FLAG" != "enabled" ] && [ "$MODE" = "gcp" ]; then
  log "local mode active — trigger container defers catch-up"; exit 0
fi

NOW="${CATCHUP_NOW:-$(date +%H:%M)}"
NOW_H="${NOW%%:*}"; NOW_M="${NOW##*:}"
NOW_MIN=$((10#$NOW_H * 60 + 10#$NOW_M))

# channel -> Postiz providerIdentifier
provider_of() {
  case "$1" in
    linkedin) echo "linkedin-page" ;;
    *)        echo "$1" ;;
  esac
}

# Posts a provider PUBLISHED today (empty on any DB trouble -> caller skips).
published_today() {
  docker exec "${POSTIZ_PG_CONTAINER:-postiz-postgres}" psql \
    -U "${POSTIZ_PG_USER:-postiz-user}" -d "${POSTIZ_PG_DB:-postiz-db-local}" -t -A -c \
    "select count(*) from \"Post\" p join \"Integration\" i on i.id=p.\"integrationId\"
     where i.\"providerIdentifier\"='$1' and p.state='PUBLISHED'
       and p.\"createdAt\"::date=current_date;" 2>/dev/null | tr -d '[:space:]'
}

fake_count() {  # CATCHUP_COUNTS="linkedin=2,x=0"
  echo ",${CATCHUP_COUNTS:-}," | grep -oE ",$1=[0-9]+," | grep -oE '[0-9]+' | head -1
}

total=0
sched_rows | while IFS=$'\t' read -r channel count delay tier cron; do
  # fires already due today: cron minute + hour list, compared to NOW
  cmin="$(echo "$cron" | awk '{print $1}')"
  hours="$(echo "$cron" | awk '{print $2}')"
  case "$cmin$hours" in (*[!0-9,]*) log "$channel: non-numeric cron ($cron) — skip"; continue ;; esac
  due=0
  for h in ${hours//,/ }; do
    fire_min=$((10#$h * 60 + 10#$cmin))
    [ "$fire_min" -le "$NOW_MIN" ] && due=$((due + count))
  done
  [ "$due" -eq 0 ] && { log "$channel: no fires due yet today"; continue; }

  if [ -n "${CATCHUP_COUNTS:-}" ]; then
    got="$(fake_count "$channel")"
  else
    got="$(published_today "$(provider_of "$channel")")"
  fi
  if [ -z "$got" ]; then log "$channel: DB unreachable — refusing to post blind"; continue; fi

  missed=$((due - got))
  if [ "$missed" -le 0 ]; then log "$channel: due=$due published=$got — on track"; continue; fi
  log "$channel: due=$due published=$got -> catching up $missed (delay ${delay})"
  if [ "${CATCHUP_DRY:-0}" = "1" ]; then
    log "DRY: would run: run-daily.sh $channel $missed"
  else
    "$HERE/run-daily.sh" "$channel" "$missed" >> "$REPO_ROOT/data/daily.log" 2>&1
  fi
done
log "done"
