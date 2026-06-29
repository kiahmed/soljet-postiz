#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Postiz Deploy ==="

# Check .env exists
if [ ! -f .env ]; then
  echo "ERROR: .env file not found. Copy .env.example and configure it first."
  exit 1
fi

# Check Docker
if ! command -v docker &>/dev/null; then
  echo "ERROR: Docker not found."
  exit 1
fi

# Create dynamicconfig dir if needed
mkdir -p dynamicconfig
[ -f dynamicconfig/development-sql.yaml ] || echo "{}" > dynamicconfig/development-sql.yaml

echo "Pulling latest images..."
docker compose pull

echo "Starting services..."
docker compose up -d

echo ""
echo "Waiting for services to be healthy..."
# Wait for postgres
for i in $(seq 1 30); do
  if docker compose exec -T postiz-postgres pg_isready -U postiz-user -d postiz-db-local &>/dev/null; then
    echo "  PostgreSQL: ready"
    break
  fi
  [ "$i" -eq 30 ] && echo "  WARNING: PostgreSQL not ready after 30s"
  sleep 1
done

# Wait for redis
for i in $(seq 1 30); do
  if docker compose exec -T postiz-redis redis-cli ping &>/dev/null; then
    echo "  Redis: ready"
    break
  fi
  [ "$i" -eq 30 ] && echo "  WARNING: Redis not ready after 30s"
  sleep 1
done

# Wait for postiz app
for i in $(seq 1 60); do
  if curl -sf http://localhost:4007 &>/dev/null; then
    echo "  Postiz app: ready"
    break
  fi
  [ "$i" -eq 60 ] && echo "  WARNING: Postiz app not responding after 60s"
  sleep 2
done

echo ""
echo "=== Deployment Complete ==="
echo "Postiz UI:    http://localhost:4007"
echo "Temporal UI:  http://localhost:8080"
echo ""
echo "Run 'docker compose logs -f postiz' to watch logs"
