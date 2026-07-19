#!/usr/bin/env bash
# What the scheduler fires once a day: heal the Temporal<->Postiz connectivity
# first (so posts don't queue into a void), then publish the day's posts.
# daily.py has its own stuck-post safety net, so a non-zero heal isn't fatal.
set -uo pipefail
cd /app || exit 1

echo "===== daily run $(date -u +%FT%TZ) ====="
make heal || echo "[warn] heal returned non-zero — continuing; daily.py will flag any stuck posts"
# Robotics only, rendered cards only. X bills ~$0.20 per post, so the daily cron
# deliberately does NOT post the parent tier: its ~2k findings are entry links
# with no card image, and putting them on a paid channel would drain credits on
# the weakest content. Run the parent by hand (`make post TIER=arboryx`) when
# you want it. READY=1 is belt-and-braces — card tiers already refuse an
# unrendered card — but it keeps the intent visible here.
make post READY=1 TIER=arboryx.robotics
echo "===== end     $(date -u +%FT%TZ) ====="
