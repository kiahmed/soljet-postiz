#!/usr/bin/env bash
# Reclaim Docker disk space safely. Does NOT touch named volumes (Postgres/Redis data
# are preserved) and does NOT remove images for other projects.
#
# Usage:
#   ./cleanup.sh           # safe: dangling images + build cache
#   ./cleanup.sh --stopped # also remove stopped containers
#   ./cleanup.sh --deep    # also remove images NOT used by any running container
#                          # (may re-pull on next deploy; re-run ./deploy.sh to restore)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

MODE="safe"
case "${1:-}" in
  ""|--safe) MODE="safe" ;;
  --stopped) MODE="stopped" ;;
  --deep)    MODE="deep" ;;
  -h|--help)
    sed -n '2,9p' "$0"; exit 0 ;;
  *) echo "Unknown flag: $1 (use --safe, --stopped, --deep)"; exit 1 ;;
esac

echo "=== Docker Cleanup ($MODE mode) ==="
echo ""
echo "--- Before ---"
docker system df
echo ""

echo "1) Removing dangling images (untagged <none>:<none>)..."
docker image prune -f

echo ""
echo "2) Pruning build cache..."
docker builder prune -f

if [ "$MODE" = "stopped" ] || [ "$MODE" = "deep" ]; then
  echo ""
  echo "3) Removing stopped containers..."
  docker container prune -f
fi

if [ "$MODE" = "deep" ]; then
  echo ""
  echo "4) Removing images not used by any RUNNING container..."
  echo "   (images tied to other projects' containers WILL be removed if those containers aren't up)"
  read -p "   Continue? [y/N] " confirm
  if [[ "$confirm" =~ ^[Yy]$ ]]; then
    docker image prune -a -f
  else
    echo "   Skipped."
  fi
fi

echo ""
echo "--- After ---"
docker system df
echo ""
echo "Postiz stack still healthy:"
docker compose ps --format "table {{.Name}}\t{{.Status}}" | head -12
