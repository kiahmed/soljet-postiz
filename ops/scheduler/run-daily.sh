#!/usr/bin/env bash
# What the scheduler fires once a day: heal the Temporal<->Postiz connectivity
# first (so posts don't queue into a void), then publish the day's posts.
# daily.py has its own stuck-post safety net, so a non-zero heal isn't fatal.
set -uo pipefail
cd /app || exit 1

echo "===== daily run $(date -u +%FT%TZ) ====="
make heal || echo "[warn] heal returned non-zero — continuing; daily.py will flag any stuck posts"
make post
echo "===== end     $(date -u +%FT%TZ) ====="
