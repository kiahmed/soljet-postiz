#!/usr/bin/env bash
# What the scheduler fires per run: heal the Temporal<->Postiz connectivity first
# (so posts don't queue into a void), then publish. daily.py has its own
# stuck-post safety net, so a non-zero heal isn't fatal.
#
# Called two ways:
#   run-daily.sh                       -> all enabled channels, knobs from env/.env
#   run-daily.sh CHANNEL COUNT DELAY TIER  -> one channel (the per-channel daily
#                                             run; both the local crontab and the
#                                             GCP trigger call it this way)
# Any positional arg may be '-' to fall through to the env/.env/default for it.
#
# Knob precedence: positional arg > real env var > .env > built-in default.
#   DAILY_POST_COUNT   cards per run   (default 1)
#   DAILY_POST_DELAY   between cards   (default 90; accepts 90 / 60m / 2h)
#   DAILY_POST_TIER    tier to post    (default arboryx.robotics; empty = ALL)
#
# Cost note: X bills ~$0.20 per post, so COUNT is a money dial.
# Set SCHEDULER_DRY_RUN=1 to print the resolved `make post` line and exit
# (no heal, no posting) — used to test knob resolution without spending.
set -uo pipefail
cd /app 2>/dev/null || cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)" || exit 1

# Positional args (a literal '-' or empty means "not supplied here").
ARG_CHANNEL="${1:-}"; [ "$ARG_CHANNEL" = "-" ] && ARG_CHANNEL=""
ARG_COUNT="${2:-}";   [ "$ARG_COUNT"   = "-" ] && ARG_COUNT=""
ARG_DELAY="${3:-}";   [ "$ARG_DELAY"   = "-" ] && ARG_DELAY=""
ARG_TIER="${4:-}";    [ "$ARG_TIER"    = "-" ] && ARG_TIER=""

# Read one var from .env without sourcing the whole file (values may hold
# spaces/quotes/#, which `source` would mangle or execute).
envget() {
  [ -f ./.env ] || return 0
  grep -E "^$1=" ./.env 2>/dev/null | tail -1 | cut -d= -f2- \
    | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" -e 's/\r$//'
}

COUNT="${ARG_COUNT:-${DAILY_POST_COUNT:-$(envget DAILY_POST_COUNT)}}"; COUNT="${COUNT:-1}"
DELAY="${ARG_DELAY:-${DAILY_POST_DELAY:-$(envget DAILY_POST_DELAY)}}"; DELAY="${DELAY:-90}"
CHANNEL="$ARG_CHANNEL"   # channel is per-run only; no env fallback (all-channels when empty)

# TIER: positional wins; else unset env -> default robotics; set-but-empty -> all tiers.
if [ -n "$ARG_TIER" ]; then TIER="$ARG_TIER"
elif [ -n "${DAILY_POST_TIER+x}" ]; then TIER="$DAILY_POST_TIER"
else TIER="$(envget DAILY_POST_TIER)"; TIER="${TIER-}"
     [ -z "$TIER" ] && ! grep -qE '^DAILY_POST_TIER=' ./.env 2>/dev/null \
       && TIER="arboryx.robotics"
fi

case "$COUNT" in ''|*[!0-9]*) echo "[warn] COUNT='$COUNT' not a number — using 1"; COUNT=1;; esac
case "$DELAY" in ''|*[!0-9smhd]*) echo "[warn] DELAY='$DELAY' not a duration — using 90"; DELAY=90;; esac

# The daily run always drains the OLDEST ready-rendered backlog first.
POST_ARGS=(READY=1 OLDEST=1 COUNT="$COUNT" DELAY="$DELAY")
[ -n "$CHANNEL" ] && POST_ARGS+=(CHANNEL="$CHANNEL")
[ -n "$TIER" ]    && POST_ARGS+=(TIER="$TIER")

if [ "${SCHEDULER_DRY_RUN:-}" = "1" ]; then
  echo "[dry-run] channel=${CHANNEL:-<all enabled>} count=$COUNT delay=$DELAY tier=${TIER:-<all enabled>}"
  echo "[dry-run] make post ${POST_ARGS[*]}"
  exit 0
fi

echo "===== daily run $(date -u +%FT%TZ)  channel=${CHANNEL:-<all>} tier=${TIER:-<all>} ====="
case "${CHANNEL:-all}" in
  linkedin) COSTNOTE="(LinkedIn — free)" ;;
  *)        COSTNOTE="(~\$$(awk "BEGIN{printf \"%.2f\", $COUNT*0.20}") on X)" ;;
esac
echo "[config] COUNT=$COUNT DELAY=$DELAY  $COSTNOTE"
make heal || echo "[warn] heal returned non-zero — continuing; daily.py will flag any stuck posts"
make post "${POST_ARGS[@]}"
echo "===== end     $(date -u +%FT%TZ) ====="
