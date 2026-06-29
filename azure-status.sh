#!/usr/bin/env bash
# Show status of the Azure-hosted Postiz deployment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

[ -f .env ] || { echo "ERROR: .env not found"; exit 1; }
set -a; source .env; set +a

: "${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
: "${AZURE_VM_NAME:?AZURE_VM_NAME is required}"
: "${AZURE_VM_ADMIN_USER:?AZURE_VM_ADMIN_USER is required}"

echo "=== Postiz Azure Status ==="
echo ""

if [ -n "${AZURE_SUBSCRIPTION_ID:-}" ]; then
  az account set --subscription "$AZURE_SUBSCRIPTION_ID" 2>/dev/null || true
fi

echo "--- Resource group ---"
if ! az group show -n "$AZURE_RESOURCE_GROUP" &>/dev/null; then
  echo "  $AZURE_RESOURCE_GROUP: NOT FOUND (run ./azure-deploy.sh)"
  exit 0
fi
echo "  $AZURE_RESOURCE_GROUP: exists in $(az group show -n "$AZURE_RESOURCE_GROUP" --query location -o tsv)"

echo ""
echo "--- VM ---"
if ! az vm show -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_VM_NAME" &>/dev/null; then
  echo "  $AZURE_VM_NAME: NOT FOUND"
  exit 0
fi

POWER=$(az vm get-instance-view -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_VM_NAME" \
        --query "instanceView.statuses[?starts_with(code,'PowerState/')].displayStatus | [0]" -o tsv)
SIZE=$(az vm show -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_VM_NAME" --query hardwareProfile.vmSize -o tsv)
PUBLIC_IP=$(az vm show -d -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_VM_NAME" --query publicIps -o tsv)
PUBLIC_FQDN=$(az vm show -d -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_VM_NAME" --query fqdns -o tsv)
PUBLIC_HOST="${PUBLIC_FQDN:-$PUBLIC_IP}"

echo "  Name:   $AZURE_VM_NAME"
echo "  Size:   $SIZE"
echo "  Power:  $POWER"
echo "  IP:     $PUBLIC_IP"
echo "  FQDN:   ${PUBLIC_FQDN:-<none>}"

if [ "$POWER" != "VM running" ]; then
  echo ""
  echo "VM is not running — skipping HTTP/SSH checks"
  exit 0
fi

echo ""
echo "--- SSH ---"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8 -o BatchMode=yes)
if ssh "${SSH_OPTS[@]}" "$AZURE_VM_ADMIN_USER@$PUBLIC_IP" "echo ok" &>/dev/null; then
  echo "  reachable as $AZURE_VM_ADMIN_USER@$PUBLIC_IP"
else
  echo "  NOT REACHABLE"
fi

echo ""
echo "--- Remote container status ---"
ssh "${SSH_OPTS[@]}" "$AZURE_VM_ADMIN_USER@$PUBLIC_IP" \
    "cd ${AZURE_REMOTE_APP_DIR:-/opt/postiz} && docker compose ps 2>/dev/null" || \
    echo "  could not query docker compose on remote"

echo ""
echo "--- HTTP health ---"
POSTIZ_URL="http://${PUBLIC_HOST}:${POSTIZ_PORT:-4007}"
TEMPORAL_URL="http://${PUBLIC_HOST}:8080"
PCODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "$POSTIZ_URL" || echo "000")
TCODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "$TEMPORAL_URL" || echo "000")
echo "  Postiz   $POSTIZ_URL  -> $PCODE"
echo "  Temporal $TEMPORAL_URL  -> $TCODE"
