#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Postiz Status ==="
echo ""

docker compose ps

echo ""
echo "--- Health Checks ---"

# Postgres
if docker compose exec -T postiz-postgres pg_isready -U postiz-user -d postiz-db-local &>/dev/null; then
  echo "PostgreSQL: healthy"
else
  echo "PostgreSQL: NOT READY"
fi

# Redis
if docker compose exec -T postiz-redis redis-cli ping &>/dev/null 2>&1; then
  echo "Redis: healthy"
else
  echo "Redis: NOT READY"
fi

# Postiz App
if curl -sf http://localhost:4007 &>/dev/null; then
  echo "Postiz App: responding (http://localhost:4007)"
else
  echo "Postiz App: NOT RESPONDING"
fi

# Temporal UI
if curl -sf http://localhost:8080 &>/dev/null; then
  echo "Temporal UI: responding (http://localhost:8080)"
else
  echo "Temporal UI: NOT RESPONDING"
fi
