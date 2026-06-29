#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Postiz Teardown ==="

read -p "This will stop and remove all Postiz containers. Data volumes will be PRESERVED. Continue? [y/N] " confirm
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
  echo "Cancelled."
  exit 0
fi

docker compose down

echo ""
echo "Containers removed. Data volumes preserved."
echo "To also delete all data: docker compose down -v"
