#!/usr/bin/env bash
# What the scheduler fires each day: heal the Temporal<->Postiz connectivity
# first (so posts don't queue into a void), then publish the day's posts.
# daily.py has its own stuck-post safety net, so a non-zero heal isn't fatal.
#
# HOW MANY POSTS PER RUN is configurable — don't edit this script to change it.
# Precedence: real env var (docker-compose `environment:`) > .env > default.
#
#   DAILY_POST_COUNT   how many cards per run   (default 1)
#   DAILY_POST_DELAY   seconds between cards    (default 90) — X rate limits
#   DAILY_POST_TIER    tier to post             (default arboryx.robotics;
#                                                set empty for ALL enabled tiers)
#
# Cost note: X bills ~$0.20 per post, so COUNT is a money dial. The default tier
# is robotics deliberately — the parent's ~2k findings are entry links with no
# card image, and they can thread (2 parts = 2 writes = $0.40).
set -uo pipefail
cd /app || exit 1

# Read one var from .env without sourcing the whole file (values there may
# contain spaces/quotes/#, which `source` would mangle or execute).
envget() {
  [ -f /app/.env ] || return 0
  grep -E "^$1=" /app/.env 2>/dev/null | tail -1 | cut -d= -f2- \
    | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" -e 's/\r$//'
}

COUNT="${DAILY_POST_COUNT:-$(envget DAILY_POST_COUNT)}"; COUNT="${COUNT:-1}"
DELAY="${DAILY_POST_DELAY:-$(envget DAILY_POST_DELAY)}"; DELAY="${DELAY:-90}"
# TIER: unset -> default robotics. Explicitly set-but-empty -> all tiers.
if [ -n "${DAILY_POST_TIER+x}" ]; then TIER="$DAILY_POST_TIER"
else TIER="$(envget DAILY_POST_TIER)"; TIER="${TIER-}"
     [ -z "$TIER" ] && ! grep -qE '^DAILY_POST_TIER=' /app/.env 2>/dev/null \
       && TIER="arboryx.robotics"
fi

case "$COUNT" in ''|*[!0-9]*) echo "[warn] DAILY_POST_COUNT='$COUNT' not a number — using 1"; COUNT=1;; esac
case "$DELAY" in ''|*[!0-9]*) echo "[warn] DAILY_POST_DELAY='$DELAY' not a number — using 90"; DELAY=90;; esac

echo "===== daily run $(date -u +%FT%TZ) ====="
echo "[config] COUNT=$COUNT DELAY=${DELAY}s TIER=${TIER:-<all enabled tiers>}  (~\$$(awk "BEGIN{printf \"%.2f\", $COUNT*0.20}") on X)"
make heal || echo "[warn] heal returned non-zero — continuing; daily.py will flag any stuck posts"
make post READY=1 COUNT="$COUNT" DELAY="$DELAY" ${TIER:+TIER="$TIER"}
echo "===== end     $(date -u +%FT%TZ) ====="
