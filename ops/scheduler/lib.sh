#!/usr/bin/env bash
# Shared helpers for the scheduler tooling. Source this, don't execute it.
# Single job: parse ops/scheduler/channels.conf into normalized, TAB-separated
# rows so every backend (local crontab, GCP jobs) reads the schedule identically.

# Repo root = two levels up from this file (ops/scheduler/lib.sh).
SCHED_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCHED_LIB_DIR/../.." && pwd)"
CHANNELS_CONF="${CHANNELS_CONF:-$SCHED_LIB_DIR/channels.conf}"

# Read one var from .env without sourcing it (values may hold spaces/#/quotes).
sched_envget() {
  local f="$REPO_ROOT/.env"
  [ -f "$f" ] || return 0
  grep -E "^$1=" "$f" 2>/dev/null | tail -1 | cut -d= -f2- \
    | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" -e 's/\r$//'
}

# Emit valid schedule rows as: channel<TAB>count<TAB>delay<TAB>tier<TAB>cron
# Trims whitespace, skips comments/blanks, validates count is numeric and the
# cron is 5 fields. Aborts (rc 1) on a malformed row so a typo can't silently
# drop a channel.
sched_rows() {
  [ -f "$CHANNELS_CONF" ] || { echo "ERROR: no channels.conf at $CHANNELS_CONF" >&2; return 1; }
  local line channel count delay tier cron n=0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"                      # strip trailing comments
    [ -z "${line//[[:space:]]/}" ] && continue   # skip blank/whitespace-only
    IFS='|' read -r channel count delay tier cron <<<"$line"
    # trim each field
    channel="$(echo "$channel" | xargs)"
    count="$(echo "$count"     | xargs)"
    delay="$(echo "$delay"     | xargs)"
    tier="$(echo "$tier"       | xargs)"
    cron="$(echo "$cron"       | xargs)"
    [ -z "$channel" ] && continue
    case "$count" in ''|*[!0-9]*) echo "ERROR: channels.conf: count '$count' for '$channel' is not a number" >&2; return 1;; esac
    case "$delay" in ''|*[!0-9smhd]*) echo "ERROR: channels.conf: delay '$delay' for '$channel' is not a duration (90, 60m, 2h)" >&2; return 1;; esac
    [ -z "$tier" ] && { echo "ERROR: channels.conf: missing tier for '$channel'" >&2; return 1; }
    if [ "$(echo "$cron" | wc -w)" -ne 5 ]; then
      echo "ERROR: channels.conf: cron '$cron' for '$channel' must be 5 fields (min hour dom mon dow)" >&2; return 1
    fi
    printf '%s\t%s\t%s\t%s\t%s\n' "$channel" "$count" "$delay" "$tier" "$cron"
    n=$((n+1))
  done < "$CHANNELS_CONF"
  [ "$n" -gt 0 ] || { echo "ERROR: channels.conf has no channel rows" >&2; return 1; }
}
