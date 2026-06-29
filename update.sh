#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Postiz Update ==="

echo "Pulling latest images..."
docker compose pull

echo "Recreating containers with new images..."
docker compose up -d --force-recreate

echo ""
echo "Pruning dangling images left behind by the pull..."
docker image prune -f 2>&1 | tail -3

echo ""
echo "Waiting for Postiz frontend..."
for i in $(seq 1 60); do
  if curl -sf http://localhost:4007 &>/dev/null; then break; fi
  sleep 2
done

# Backend can hang on cold start if Temporal wasn't ready yet.
# Verify it's listening; restart the pm2 backend process inside the container if not.
echo "Verifying Postiz backend (port 3000 inside container)..."
for i in $(seq 1 30); do
  if docker exec postiz sh -c "ss -tln | grep -q ':3000 '" 2>/dev/null; then
    echo "Backend listening on :3000"
    break
  fi
  if [ "$i" -eq 15 ]; then
    echo "Backend not listening after 30s — kicking pm2 restart..."
    docker exec postiz pm2 restart backend >/dev/null 2>&1 || true
  fi
  [ "$i" -eq 30 ] && echo "WARNING: backend still not listening. Check: docker exec postiz pm2 logs backend"
  sleep 2
done

# Smoke test via the tunnel (or localhost)
CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://localhost:4007/auth || echo "000")
echo "Postiz /auth -> HTTP $CODE"
if [ "$CODE" = "200" ]; then
  echo "Postiz is back online at http://localhost:4007"
  exit 0
fi

echo "WARNING: Postiz /auth did not return 200. Check logs with: docker compose logs -f postiz"
exit 1
