#!/usr/bin/env bash
# SSH into the Azure VM, pull latest images, and recreate containers.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

[ -f .env ] || { echo "ERROR: .env not found"; exit 1; }
set -a; source .env; set +a

: "${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
: "${AZURE_VM_NAME:?AZURE_VM_NAME is required}"
: "${AZURE_VM_ADMIN_USER:?AZURE_VM_ADMIN_USER is required}"

echo "=== Postiz Azure Update ==="

if [ -n "${AZURE_SUBSCRIPTION_ID:-}" ]; then
  az account set --subscription "$AZURE_SUBSCRIPTION_ID" 2>/dev/null || true
fi

PUBLIC_IP=$(az vm show -d -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_VM_NAME" --query publicIps -o tsv)
[ -n "$PUBLIC_IP" ] || { echo "ERROR: could not resolve VM public IP"; exit 1; }

REMOTE_DIR="${AZURE_REMOTE_APP_DIR:-/opt/postiz}"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)

echo "Pulling latest images on $PUBLIC_IP..."
ssh "${SSH_OPTS[@]}" "$AZURE_VM_ADMIN_USER@$PUBLIC_IP" \
    "cd $REMOTE_DIR && docker compose pull && docker compose up -d --force-recreate"

echo ""
echo "Waiting for Postiz to come back up..."
PORT="${POSTIZ_PORT:-4007}"
for i in $(seq 1 60); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://${PUBLIC_IP}:${PORT}" || echo "000")
  if [ "$CODE" = "200" ] || [ "$CODE" = "307" ] || [ "$CODE" = "302" ]; then
    echo "Postiz is back online at http://${PUBLIC_IP}:${PORT} (HTTP $CODE)"
    exit 0
  fi
  sleep 2
done

echo "WARNING: Postiz not responding after 120s. Check with:"
echo "  ssh $AZURE_VM_ADMIN_USER@$PUBLIC_IP 'cd $REMOTE_DIR && docker compose logs -f postiz'"
exit 1
