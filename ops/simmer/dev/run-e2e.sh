#!/usr/bin/env bash
# End-to-end for the Simmer poster, all local:
#   Pub/Sub emulator (docker) + stub EdgeLane API + stub simmer-snap + real Postiz
#   (drafts only — nothing is published).
#
#   ops/simmer/dev/run-e2e.sh            full run + teardown
#   ops/simmer/dev/run-e2e.sh --keep     leave stubs + emulator up afterwards
set -uo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"   # worktree root

PY=.venv/bin/python
KEEP=0; [ "${1:-}" = "--keep" ] && KEEP=1
EMU_HOST="localhost:8681"
API_PORT=8899 SNAP_PORT=8898
pids=()
fail=0
say(){ printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok(){  printf '  \033[32mPASS\033[0m %s\n' "$*"; }
no(){  printf '  \033[31mFAIL\033[0m %s\n' "$*"; fail=1; }

cleanup(){
  [ "$KEEP" = 1 ] && { echo "--keep: leaving stubs+emulator up"; return; }
  say "teardown"
  for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done
  PUBSUB_EMULATOR_HOST="$EMU_HOST" GCP_PROJECT=marketresearch-agents \
    $PY ops/simmer/dev/emu.py purge 2>/dev/null || true
  docker compose -f ops/simmer/dev/docker-compose.yml down -t2 2>/dev/null || true
}
trap cleanup EXIT

# ---------------------------------------------------------------- emulator
say "pub/sub emulator"
docker compose -f ops/simmer/dev/docker-compose.yml up -d pubsub
for i in $(seq 1 40); do
  docker exec simmer-pubsub-emu curl -sf http://localhost:8085 >/dev/null 2>&1 && break
  sleep 1
done
export PUBSUB_EMULATOR_HOST="$EMU_HOST" GCP_PROJECT=marketresearch-agents
$PY ops/simmer/dev/emu.py setup

# ---------------------------------------------------------------- stubs
say "stubs"
SIMMER_API_TOKEN=dev-simmer-token STUB_API_PORT=$API_PORT $PY ops/simmer/dev/stub_api.py &  pids+=($!)
STUB_SNAP_PORT=$SNAP_PORT $PY ops/simmer/dev/stub_snap.py &                                  pids+=($!)
for i in $(seq 1 20); do
  curl -sf "http://localhost:$API_PORT/healthz" >/dev/null 2>&1 \
    && curl -sf "http://localhost:$SNAP_PORT/healthz" >/dev/null 2>&1 && break
  sleep 0.5
done
curl -sf "http://localhost:$API_PORT/healthz" >/dev/null && ok "stub api up"   || no "stub api down"
curl -sf "http://localhost:$SNAP_PORT/healthz" >/dev/null && ok "stub snap up" || no "stub snap down"

# point the source + snap at the stubs for this run
export SIMMER_API_BASE="http://localhost:$API_PORT" \
       SIMMER_SNAP_URL="http://localhost:$SNAP_PORT/snap" \
       SIMMER_API_TOKEN=dev-simmer-token \
       SIMMER_DEDUPE_BACKEND=local

rm -f data/simmer_poster_dedupe.json

# ------------------------------------------------- 1. publish + pull (dry-run)
say "publish 3 events (matrix one must be filtered out by the subscription)"
$PY ops/simmer/dev/emu.py publish --product simmer --symbol MSTR --state ready        --expiry 2026-09-19 --event-id e2e-mstr-ready
$PY ops/simmer/dev/emu.py publish --product simmer --symbol TSLA --state watch_entered --expiry 2026-09-19 --event-id e2e-tsla-watch
$PY ops/simmer/dev/emu.py publish --product matrix --symbol NVDA --state ready        --expiry 2026-09-19 --event-id e2e-matrix

say "poster --pull --dry-run"
OUT=$($PY bin/simmer_poster.py --pull --dry-run --max 10 --timeout 15 2>&1); echo "$OUT"
grep -q '"symbol": "MSTR", "state": "ready", "status": "dry-run"' <<<"$OUT" && ok "MSTR ready composed" || no "MSTR ready missing"
grep -q '"symbol": "TSLA", "state": "watch_entered", "status": "dry-run"' <<<"$OUT" && ok "TSLA watch composed" || no "TSLA watch missing"
grep -q 'NVDA' <<<"$OUT" && no "matrix event leaked past the subscription filter" || ok "matrix event filtered out"
grep -q 'simmer_snap_' <<<"$OUT" && ok "snap image attached" || no "no snap image in bundle"

# ------------------------------------------------- 2. real Postiz draft
say "poster --event --mode draft  (real Postiz draft to the Simmer LinkedIn channel)"
EVT="$(mktemp)"
printf '{"product":"simmer","symbol":"MSTR","state":"ready","expiry":"2026-09-19","event_id":"e2e-draft-1"}' > "$EVT"
OUT=$($PY bin/simmer_poster.py --event "@$EVT" --mode draft 2>&1); echo "$OUT"
grep -q '"status": "posted"' <<<"$OUT" && ok "draft posted" || no "draft not posted"
grep -q '"state": "DRAFT"' <<<"$OUT"  && ok "channel state DRAFT" || no "channel not DRAFT"

say "poster --event again  (dedupe must block it)"
OUT=$($PY bin/simmer_poster.py --event "@$EVT" --mode draft 2>&1); echo "$OUT"
grep -q '"status": "duplicate"' <<<"$OUT" && ok "replay blocked by dedupe" || no "replay NOT blocked"

# ------------------------------------------------- 3. status parity
say "status tooling shows simmer per channel"
# capture first, then match — `grep -q` inside a pipeline with `set -o pipefail`
# SIGPIPEs the producer and the pipeline reads as a failure.
CHK="$($PY bin/daily.py --check 2>&1)"; echo "$CHK"
grep -qE '^[[:space:]]*simmer[[:space:]]' <<<"$CHK" && ok "daily.py --check lists simmer" || no "simmer missing from --check"
SS="$($PY bin/social-status.py --tier simmer 2>&1)"; echo "$SS"
grep -qiE 'simmer|linkedin' <<<"$SS" && ok "social-status has simmer" || no "social-status missing simmer"

echo
[ "$fail" = 0 ] && echo -e "\033[32mE2E PASSED\033[0m" || echo -e "\033[31mE2E FAILED\033[0m"
exit $fail
