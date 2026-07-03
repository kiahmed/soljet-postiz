#!/usr/bin/env bash
# Heal Temporal <-> Postiz connectivity.
#
# The recurring failure mode: Temporal is up and the Postiz orchestrator shows
# "online", but its per-provider workers have silently stopped polling their
# task queues — so every queued post sits in QUEUE forever and nothing
# publishes. Restarting the postiz container re-registers the workers.
#
# This script checks the chain and re-registers only what's broken:
#   1. Temporal cluster SERVING?      -> if not, restart temporal, wait healthy
#   2. Workers polling their queues?  -> if not, restart postiz, wait for pollers
#
# Usage:
#   bin/heal.sh            # check, and repair anything unhealthy
#   bin/heal.sh --check    # report only, never restart (exit 1 if unhealthy)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

ADMIN="${POSTIZ_TEMPORAL_CONTAINER:-temporal-admin-tools}"
QUEUES=("${@:2}")
[ "$CHECK_ONLY" -eq 1 ] && QUEUES=()
[ ${#QUEUES[@]} -eq 0 ] && QUEUES=(x linkedin)   # the providers we actually use

say() { echo "[heal] $*"; }

temporal_serving() {
  docker exec "$ADMIN" tctl --address temporal:7233 cluster health 2>/dev/null \
    | grep -q SERVING
}

# echoes the number of live pollers on a task queue
pollers() {
  docker exec "$ADMIN" tctl --address temporal:7233 taskqueue describe \
    --taskqueue "$1" --taskqueuetype workflow 2>/dev/null | grep -c '@'
}

workers_polling() {
  for q in "${QUEUES[@]}"; do
    [ "$(pollers "$q")" -gt 0 ] || return 1
  done
  return 0
}

# ---- 1. Temporal ---------------------------------------------------------
if temporal_serving; then
  say "Temporal cluster: SERVING"
else
  say "Temporal cluster: NOT serving"
  if [ "$CHECK_ONLY" -eq 1 ]; then exit 1; fi
  say "restarting temporal..."
  docker compose restart temporal >/dev/null
  for i in $(seq 1 24); do
    sleep 5
    if temporal_serving; then say "Temporal recovered (${i}x5s)"; break; fi
    [ "$i" -eq 24 ] && { say "Temporal still not serving after 2m — aborting"; exit 1; }
  done
fi

# ---- 2. Workers ----------------------------------------------------------
if workers_polling; then
  say "Workers polling: YES (${QUEUES[*]})"
  exit 0
fi

say "Workers polling: NO — queues without pollers among: ${QUEUES[*]}"
if [ "$CHECK_ONLY" -eq 1 ]; then exit 1; fi

say "restarting postiz to re-register workers..."
docker compose restart postiz >/dev/null
for i in $(seq 1 24); do   # workers re-bundle in ~1-3 min
  sleep 8
  if workers_polling; then
    say "Workers re-registered after $((i*8))s — healthy"
    exit 0
  fi
done
say "Workers still not polling after ~3m. Check: docker exec postiz pm2 logs orchestrator"
exit 1
