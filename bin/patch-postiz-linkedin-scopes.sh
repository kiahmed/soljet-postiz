#!/usr/bin/env bash
# Drop scopes from Postiz's compiled LinkedIn providers that LinkedIn won't
# grant to a Community Management API app. LinkedIn policy (mid-2026): CM API
# apps can't coexist with "Sign In with LinkedIn using OpenID Connect", so
# `openid` and `profile` are permanently unavailable to our app. Postiz
# hard-codes them in its scope list, causing `unauthorized_scope_error` on
# every Connect attempt.
#
# Patches four compiled JS files in the running container. Idempotent.
# Re-run after `docker compose up -d --force-recreate postiz`.
#
# To undo: `docker compose up -d --force-recreate postiz` (resets to image).
set -euo pipefail

FILES=(
    /app/libraries/nestjs-libraries/src/integrations/social/linkedin.provider.ts
    /app/libraries/nestjs-libraries/src/integrations/social/linkedin.page.provider.ts
    /app/apps/backend/dist/libraries/nestjs-libraries/src/integrations/social/linkedin.provider.js
    /app/apps/backend/dist/libraries/nestjs-libraries/src/integrations/social/linkedin.page.provider.js
    /app/apps/orchestrator/dist/libraries/nestjs-libraries/src/integrations/social/linkedin.provider.js
    /app/apps/orchestrator/dist/libraries/nestjs-libraries/src/integrations/social/linkedin.page.provider.js
)

for F in "${FILES[@]}"; do
    docker exec postiz sh -c "sed -i \"s/'openid',//g; s/'profile',//g\" '$F' 2>/dev/null || true"
    REMAINING=$(docker exec postiz sh -c "grep -c openid '$F' 2>/dev/null || echo 0")
    echo "  $F: openid count = $REMAINING"
done

echo ""
echo "Clearing node compile cache..."
docker exec postiz sh -c "rm -rf /app/node-compile-cache/* 2>/dev/null || true"

echo "Restarting backend + orchestrator..."
docker exec postiz pm2 restart backend orchestrator >/dev/null
echo "Done. Retry LinkedIn Connect in Postiz UI."
